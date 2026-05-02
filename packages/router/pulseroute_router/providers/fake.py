"""Deterministic in-process provider used by unit tests, eval-CI smoke, and
local dev when no provider keys are set."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

from pulseroute_shared.types import ChatCompletionRequest, ProviderName

from pulseroute_router.cost import estimate_tokens
from pulseroute_router.provider import ProviderResponse


class FakeProvider:
    name = ProviderName.FAKE
    supported_models = frozenset({"fake-small", "fake-large"})

    def __init__(self, *, fail_n_times: int = 0) -> None:
        self._fail_n_times = fail_n_times
        self._calls = 0

    async def complete(self, request: ChatCompletionRequest) -> ProviderResponse:
        self._calls += 1
        if self._calls <= self._fail_n_times:
            raise RuntimeError("simulated provider failure")
        prompt = "\n".join(m.content for m in request.messages)
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        content = f"fake[{request.model}]:{digest}"
        return ProviderResponse(
            content=content,
            prompt_tokens=sum(estimate_tokens(m.content) for m in request.messages),
            completion_tokens=estimate_tokens(content),
            raw_model=request.model,
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        response = await self.complete(request)
        for token in response.content.split(":"):
            yield token

    async def healthcheck(self) -> bool:
        return True
