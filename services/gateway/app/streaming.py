"""SSE streaming for /v1/chat/completions.

Hosts the chunk-by-chunk OpenAI-shaped emitter and the upstream-disconnect
error path. When the upstream provider's iterator raises mid-stream
(``httpx.ReadError``, ``httpx.ConnectError``, or any unexpected exception),
the gateway:
  1. Persists whatever partial tokens it accumulated to the request-log sink
     (with ``error_code='upstream_disconnected'``).
  2. Emits a structured ``error`` SSE chunk so the client knows the stream
     terminated abnormally and can decide whether to retry.
  3. Emits the terminal ``data: [DONE]`` sentinel so client SDKs that look
     for it terminate cleanly.

The order matters — persistence happens before the error chunk hits the wire
so a client-side timeout cannot race the writer. See
``ARCHITECTURE.md`` §sse-error-event-protocol.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog
from pulseroute_router.provider import ChatProvider
from pulseroute_shared.types import ChatCompletionRequest, RouteDecision

log = structlog.get_logger()


class _NullRequestLogSink:
    """No-op sink used when no real ClickHouse writer is wired (tests, dev).

    Real deployments swap this for an async ClickHouse client (the
    ``HttpClickHouseClient`` already exists for the canary path). The sink
    interface is intentionally tiny: one method, ``record(row: dict)``."""

    async def record(self, row: dict[str, Any]) -> None:  # noqa: ARG002
        return None


# Module-level sink; tests monkeypatch this to assert persistence happened.
# Production wires a real ClickHouse client at app startup.
REQUEST_LOG_SINK: Any = _NullRequestLogSink()


_DISCONNECT_EXC = (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError)


async def sse_stream(
    provider: ChatProvider,
    body: ChatCompletionRequest,
    decision: RouteDecision,
    tenant_id: str,
    request_id: str | None = None,
) -> AsyncIterator[bytes]:
    """Yield OpenAI-shaped SSE bytes for a streaming chat completion.

    On mid-stream upstream disconnect, yields a structured error chunk and
    the ``[DONE]`` sentinel. On normal completion, yields a final
    ``finish_reason=stop`` chunk and ``[DONE]``.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    partial_tokens: list[str] = []
    try:
        async for delta in provider.stream(body):
            partial_tokens.append(delta)
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": decision.chosen_model,
                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode()
    except _DISCONNECT_EXC as exc:
        async for chunk_bytes in _emit_disconnect(
            completion_id, created, decision, tenant_id, partial_tokens, exc, request_id
        ):
            yield chunk_bytes
        return
    except Exception as exc:  # pragma: no cover - any other mid-stream failure
        async for chunk_bytes in _emit_disconnect(
            completion_id, created, decision, tenant_id, partial_tokens, exc, request_id
        ):
            yield chunk_bytes
        return

    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": decision.chosen_model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


async def _emit_disconnect(
    completion_id: str,
    created: int,
    decision: RouteDecision,
    tenant_id: str,
    partial_tokens: list[str],
    exc: BaseException,
    request_id: str | None,
) -> AsyncIterator[bytes]:
    partial_completion = "".join(partial_tokens)
    # Persist before the error chunk hits the wire. If the sink raises, log
    # and continue — a logging blip must not eat the user's error event.
    try:
        await REQUEST_LOG_SINK.record(
            {
                "tenant_id": tenant_id,
                "request_id": request_id,
                "model": decision.chosen_model,
                "provider": decision.chosen_provider.value,
                "route_reason": decision.route_reason,
                "error_code": "upstream_disconnected",
                "partial_completion": partial_completion,
                "tokens_out": len(partial_tokens),
                "completion_id": completion_id,
            }
        )
    except Exception as sink_exc:  # pragma: no cover - sink errors must not propagate
        log.warning("request_log_sink_failed", error=str(sink_exc))

    err_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": decision.chosen_model,
        "choices": [],
        "error": {
            "code": "upstream_disconnected",
            "message": str(exc),
            "retryable": True,
            "request_id": request_id,
        },
    }
    yield f"data: {json.dumps(err_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"
