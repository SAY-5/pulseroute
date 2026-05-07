"""POST /v1/chat/completions and /v1/embeddings.

Streaming uses OpenAI's exact SSE event shape so existing clients work.
Non-streaming returns OpenAI's exact JSON shape plus a non-OpenAI
``pulseroute`` metadata block (clients can ignore it)."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from prometheus_client import Histogram
from pulseroute_gateway.auth import resolve_api_key
from pulseroute_gateway.metrics import (
    CACHE_LOOKUPS,
    CIRCUIT_BREAKER_STATE,
    COST_USD_TOTAL,
    PROVIDER_ERRORS,
    PROVIDER_REQUESTS,
    record_cache_lookup,
    record_gateway_added,
)
from pulseroute_gateway.tenant import get_tenant_context
from pulseroute_router.breaker import CircuitState
from pulseroute_router.cost import MODEL_PRICES
from pulseroute_router.provider import ChatProvider
from pulseroute_shared.types import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    Usage,
)

router = APIRouter(tags=["openai"])
log = structlog.get_logger()

UPSTREAM_LATENCY = Histogram(
    "provider_upstream_latency_seconds",
    "Round-trip time spent inside the upstream provider call.",
    labelnames=("provider", "model"),
)


def _select_provider(provider_name: str, providers: dict[str, ChatProvider]) -> ChatProvider:
    if provider_name in providers:
        return providers[provider_name]
    # Fall back to FakeProvider for any model whose real provider isn't wired.
    return providers["fake"]


def _route_for_request(request: Request, body: ChatCompletionRequest, tenant_ctx, deps):
    policy_name = body.pulseroute_policy_id or tenant_ctx.policy_id
    policy = deps.policies.get(policy_name) or deps.policies["quality_first"]
    decision = deps.router.decide(body, tenant_ctx, policy)
    # Keep gauge fresh for every (provider, model) we considered.
    for model in decision.candidate_models:
        provider = MODEL_PRICES[model].provider.value
        breaker = deps.router.breakers.get((MODEL_PRICES[model].provider, model))
        if breaker is not None:
            state_val = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}[
                breaker.state
            ]
            CIRCUIT_BREAKER_STATE.labels(provider=provider, model=model).set(state_val)
    return policy, decision


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
):
    deps = request.app.state.deps
    resolved = resolve_api_key(authorization)
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
    tenant_ctx = get_tenant_context(resolved.tenant_id)
    if tenant_ctx is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "unknown tenant")

    gateway_started = time.perf_counter()

    # Cache lookup honours tenant override flag.
    if not body.pulseroute_no_cache:
        cache_started = time.perf_counter()
        try:
            lookup = await deps.cache.lookup(tenant_ctx.tenant_id, body.messages)
        except Exception as exc:  # pragma: no cover - guard against Redis blips
            log.warning("cache_lookup_failed", error=str(exc))
            lookup = None
        record_cache_lookup(time.perf_counter() - cache_started)
        if lookup and lookup.hit and lookup.entry is not None:
            CACHE_LOOKUPS.labels(tenant_id=tenant_ctx.tenant_id, outcome="hit").inc()
            entry = lookup.entry
            record_gateway_added(time.perf_counter() - gateway_started)
            return ChatCompletionResponse(
                id=f"chatcmpl-{uuid.uuid4().hex}",
                created=int(entry.created_at),
                model=entry.model,
                choices=[
                    Choice(
                        index=0,
                        message=ChatMessage(role="assistant", content=entry.completion),
                        finish_reason="stop",
                    )
                ],
                usage=Usage(
                    prompt_tokens=entry.prompt_tokens,
                    completion_tokens=entry.completion_tokens,
                    total_tokens=entry.prompt_tokens + entry.completion_tokens,
                ),
                pulseroute={
                    "cache_hit": True,
                    "similarity": lookup.similarity,
                    "policy_id": tenant_ctx.policy_id,
                    "request_id": getattr(request.state, "request_id", None),
                },
            )
        CACHE_LOOKUPS.labels(tenant_id=tenant_ctx.tenant_id, outcome="miss").inc()

    policy, decision = _route_for_request(request, body, tenant_ctx, deps)

    # Override the model on the way down so providers see the chosen model.
    body_for_call = body.model_copy(update={"model": decision.chosen_model})
    provider = _select_provider(decision.chosen_provider.value, deps.providers)
    breaker = deps.router._breaker_for(decision.chosen_provider, decision.chosen_model)

    if body.stream:
        gateway_added = time.perf_counter() - gateway_started
        record_gateway_added(gateway_added)
        request_id = getattr(request.state, "request_id", None)
        return StreamingResponse(
            _sse_stream(
                provider,
                body_for_call,
                decision,
                tenant_ctx.tenant_id,
                request_id=request_id,
            ),
            media_type="text/event-stream",
        )

    upstream_started = time.perf_counter()
    try:
        PROVIDER_REQUESTS.labels(
            provider=decision.chosen_provider.value, model=decision.chosen_model
        ).inc()
        response = await provider.complete(body_for_call)
        breaker.record_success()
    except Exception as exc:
        breaker.record_failure()
        PROVIDER_ERRORS.labels(
            provider=decision.chosen_provider.value,
            model=decision.chosen_model,
            code="upstream_error",
        ).inc()
        log.warning(
            "upstream_error",
            provider=decision.chosen_provider.value,
            model=decision.chosen_model,
            error=str(exc),
        )
        # Single, structured error response — clients decide whether to retry.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": {
                    "code": "upstream_unavailable",
                    "message": "upstream provider call failed",
                    "retryable": True,
                }
            },
        ) from exc
    finally:
        UPSTREAM_LATENCY.labels(
            provider=decision.chosen_provider.value, model=decision.chosen_model
        ).observe(time.perf_counter() - upstream_started)

    # Cost accounting (estimated; real billing happens off the hot path).
    price = MODEL_PRICES[decision.chosen_model]
    cost_usd = (
        response.prompt_tokens * price.input_per_1k / 1000.0
        + response.completion_tokens * price.output_per_1k / 1000.0
    )
    COST_USD_TOTAL.labels(tenant_id=tenant_ctx.tenant_id, model=decision.chosen_model).inc(cost_usd)

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    out = ChatCompletionResponse(
        id=completion_id,
        created=int(time.time()),
        model=decision.chosen_model,
        choices=[
            Choice(
                index=0,
                message=ChatMessage(role="assistant", content=response.content),
                finish_reason=response.finish_reason or "stop",
            )
        ],
        usage=Usage(
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.prompt_tokens + response.completion_tokens,
        ),
        pulseroute={
            "cache_hit": False,
            "policy_id": tenant_ctx.policy_id,
            "route_reason": decision.route_reason,
            "candidates": decision.candidate_models,
            "cost_usd": round(cost_usd, 6),
            "request_id": getattr(request.state, "request_id", None),
        },
    )

    # Async-style cache fill (synchronous here for simplicity; safe to await).
    if not body.pulseroute_no_cache:
        try:
            await deps.cache.store(
                tenant_ctx.tenant_id,
                body.messages,
                response.content,
                decision.chosen_model,
                response.prompt_tokens,
                response.completion_tokens,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("cache_store_failed", error=str(exc))

    upstream_elapsed = time.perf_counter() - upstream_started
    total_elapsed = time.perf_counter() - gateway_started
    record_gateway_added(max(0.0, total_elapsed - upstream_elapsed))
    return out


async def _sse_stream(
    provider: ChatProvider,
    body: ChatCompletionRequest,
    decision,
    tenant_id: str,
    request_id: str | None = None,
) -> AsyncIterator[bytes]:
    """Thin wrapper around :func:`pulseroute_gateway.streaming.sse_stream`.

    Increments the ``stream_disconnect`` error metric on mid-stream failures
    by sniffing the emitted bytes for the ``upstream_disconnected`` code.
    The SSE protocol + persistence live in ``streaming.py``."""
    from pulseroute_gateway.streaming import sse_stream as _sse

    saw_error = False
    async for chunk in _sse(provider, body, decision, tenant_id, request_id=request_id):
        if not saw_error and b'"upstream_disconnected"' in chunk:
            saw_error = True
            PROVIDER_ERRORS.labels(
                provider=decision.chosen_provider.value,
                model=decision.chosen_model,
                code="stream_disconnect",
            ).inc()
        yield chunk


@router.post("/embeddings")
async def embeddings(
    request: Request, body: dict, authorization: str | None = Header(default=None)
):
    deps = request.app.state.deps  # noqa: F841 - kept for symmetry / future provider routing
    resolved = resolve_api_key(authorization)
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    # Stand-in deterministic embeddings — wire to a real provider when you ship.
    from pulseroute_cache.embeddings import HashEmbedder

    embedder = HashEmbedder()
    data = [
        {"object": "embedding", "embedding": embedder.embed(text), "index": i}
        for i, text in enumerate(inputs)
    ]
    return {
        "object": "list",
        "data": data,
        "model": body.get("model", "pulseroute-hash-embedder"),
        "usage": {"prompt_tokens": sum(len(t.split()) for t in inputs), "total_tokens": 0},
    }
