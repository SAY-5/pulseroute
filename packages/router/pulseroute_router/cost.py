"""Per-1k-token price table and a coarse cost estimator.

Prices are illustrative reference points for routing decisions only — they are
NOT a billing source of truth. Update on a release cadence."""

from __future__ import annotations

from dataclasses import dataclass

from pulseroute_shared.types import ChatCompletionRequest, ProviderName


@dataclass(frozen=True, slots=True)
class ModelPrice:
    provider: ProviderName
    model: str
    input_per_1k: float  # USD
    output_per_1k: float  # USD
    quality_score: float  # 0..1, higher is better; from internal eval suite


MODEL_PRICES: dict[str, ModelPrice] = {
    "gpt-4o": ModelPrice(ProviderName.OPENAI, "gpt-4o", 0.0050, 0.0150, 0.92),
    "gpt-4o-mini": ModelPrice(ProviderName.OPENAI, "gpt-4o-mini", 0.00015, 0.0006, 0.78),
    "claude-3-5-sonnet": ModelPrice(
        ProviderName.ANTHROPIC, "claude-3-5-sonnet", 0.003, 0.015, 0.93
    ),
    "claude-3-haiku": ModelPrice(ProviderName.ANTHROPIC, "claude-3-haiku", 0.00025, 0.00125, 0.74),
    "llama-3.1-8b-instruct": ModelPrice(
        ProviderName.VLLM, "llama-3.1-8b-instruct", 0.00005, 0.00015, 0.62
    ),
    "fake-small": ModelPrice(ProviderName.FAKE, "fake-small", 0.00001, 0.00002, 0.50),
    "fake-large": ModelPrice(ProviderName.FAKE, "fake-large", 0.0001, 0.0002, 0.85),
}


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars/token. Cheap and good enough for routing.

    The accurate path (tiktoken) is reserved for accounting after a call lands."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_request_cost(
    request: ChatCompletionRequest, model: str, expected_output_tokens: int = 256
) -> float:
    """Estimate USD cost for a request against a candidate model."""
    price = MODEL_PRICES.get(model)
    if price is None:
        return float("inf")
    prompt_tokens = sum(estimate_tokens(m.content) for m in request.messages)
    output_tokens = request.max_tokens or expected_output_tokens
    return (
        prompt_tokens * price.input_per_1k / 1000.0 + output_tokens * price.output_per_1k / 1000.0
    )
