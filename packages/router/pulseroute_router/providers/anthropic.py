"""Anthropic Messages API provider. Translates OpenAI-shaped requests to
Anthropic's schema and back."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from pulseroute_shared.types import ChatCompletionRequest, ProviderName

from pulseroute_router.provider import ProviderResponse


def _split_system(messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, str]]]:
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    chat = [m for m in messages if m["role"] != "system"]
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


class AnthropicProvider:
    name = ProviderName.ANTHROPIC
    supported_models = frozenset({"claude-3-5-sonnet", "claude-3-haiku"})

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        timeout_s: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    async def complete(self, request: ChatCompletionRequest) -> ProviderResponse:
        msgs = [m.model_dump(exclude_none=True) for m in request.messages]
        system, chat = _split_system(msgs)
        payload: dict[str, object] = {
            "model": request.model,
            "messages": chat,
            "max_tokens": request.max_tokens or 1024,
        }
        if system:
            payload["system"] = system
        resp = await self._client.post("/messages", json=payload)
        resp.raise_for_status()
        body = resp.json()
        content_blocks = body.get("content", [])
        text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        usage = body.get("usage", {})
        return ProviderResponse(
            content=text,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            finish_reason=body.get("stop_reason", "stop"),
            raw_model=body.get("model"),
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        # Anthropic SSE is delta-shaped; for the gateway we just need text deltas.
        msgs = [m.model_dump(exclude_none=True) for m in request.messages]
        system, chat = _split_system(msgs)
        payload: dict[str, object] = {
            "model": request.model,
            "messages": chat,
            "max_tokens": request.max_tokens or 1024,
            "stream": True,
        }
        if system:
            payload["system"] = system
        async with self._client.stream("POST", "/messages", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and "text_delta" in line:
                    import json as _json

                    try:
                        chunk = _json.loads(line.removeprefix("data: "))
                    except _json.JSONDecodeError:
                        continue
                    delta = chunk.get("delta", {}).get("text")
                    if delta:
                        yield delta

    async def healthcheck(self) -> bool:
        # Anthropic has no public ping; treat 401 as "reachable, just not authed"
        # to avoid flapping the breaker on a misconfigured key.
        try:
            r = await self._client.get("/")
            return r.status_code in (200, 401, 404)
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
