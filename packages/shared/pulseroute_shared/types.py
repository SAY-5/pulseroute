"""Pydantic types shared across services. Mirrors OpenAI's chat-completions schema
closely so existing client SDKs work without changes."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProviderName(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    VLLM = "vllm"
    FAKE = "fake"


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = 1.0
    top_p: float | None = 1.0
    max_tokens: int | None = None
    stream: bool = False
    user: str | None = None
    # PulseRoute extensions (ignored by upstream).
    pulseroute_policy_id: str | None = None
    pulseroute_no_cache: bool = False


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class Choice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter"] | None = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage
    # PulseRoute-only metadata.
    pulseroute: dict[str, Any] = Field(default_factory=dict)


class RouteDecision(BaseModel):
    chosen_provider: ProviderName
    chosen_model: str
    candidate_models: list[str]
    route_reason: str
    policy_id: str | None = None
    cost_cap_remaining_usd: float | None = None
