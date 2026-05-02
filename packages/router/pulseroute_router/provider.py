"""ChatProvider Protocol — every concrete provider conforms to this surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pulseroute_shared.types import ChatCompletionRequest, ProviderName


@dataclass(slots=True)
class ProviderResponse:
    """Non-streaming response shape produced by a ChatProvider.complete call."""

    content: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"
    raw_model: str | None = None


@runtime_checkable
class ChatProvider(Protocol):
    """The minimum surface a model provider must implement.

    Implementations must be async-safe and free of global state — the gateway may
    instantiate one per process and call into them from many request tasks.
    """

    name: ProviderName
    supported_models: frozenset[str]

    async def complete(self, request: ChatCompletionRequest) -> ProviderResponse: ...

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]: ...

    async def healthcheck(self) -> bool: ...
