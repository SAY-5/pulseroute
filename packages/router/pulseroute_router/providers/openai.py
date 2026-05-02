"""OpenAI Chat Completions provider. HTTP-only — no SDK dependency, so respx
can mock it cleanly in tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from pulseroute_shared.types import ChatCompletionRequest, ProviderName

from pulseroute_router.provider import ProviderResponse


class OpenAIProvider:
    name = ProviderName.OPENAI
    supported_models = frozenset({"gpt-4o", "gpt-4o-mini"})

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def complete(self, request: ChatCompletionRequest) -> ProviderResponse:
        payload = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()
        choice = body["choices"][0]
        usage = body.get("usage", {})
        return ProviderResponse(
            content=choice["message"]["content"],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", "stop"),
            raw_model=body.get("model"),
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        payload = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": True,
        }
        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ")
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = chunk["choices"][0].get("delta", {}).get("content")
                if delta:
                    yield delta

    async def healthcheck(self) -> bool:
        try:
            r = await self._client.get("/models")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
