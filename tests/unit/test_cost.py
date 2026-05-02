"""Cost estimator tests across the price table."""

from __future__ import annotations

import pytest
from pulseroute_router.cost import MODEL_PRICES, estimate_request_cost
from pulseroute_shared.types import ChatCompletionRequest, ChatMessage


def _req(text: str = "hello world", max_tokens: int | None = 256) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="x",
        messages=[ChatMessage(role="user", content=text)],
        max_tokens=max_tokens,
    )


@pytest.mark.parametrize("model", list(MODEL_PRICES.keys()))
def test_known_models_have_finite_cost(model):
    cost = estimate_request_cost(_req(), model)
    assert 0 <= cost < float("inf")


def test_unknown_model_returns_inf():
    assert estimate_request_cost(_req(), "no-such-model") == float("inf")


def test_cost_scales_with_max_tokens():
    a = estimate_request_cost(_req(max_tokens=100), "gpt-4o")
    b = estimate_request_cost(_req(max_tokens=1000), "gpt-4o")
    assert b > a


def test_cheaper_model_is_actually_cheaper():
    cheap = estimate_request_cost(_req(), "gpt-4o-mini")
    pricey = estimate_request_cost(_req(), "gpt-4o")
    assert cheap < pricey


def test_local_vllm_is_cheapest():
    costs = {m: estimate_request_cost(_req(), m) for m in MODEL_PRICES}
    cheapest = min(costs, key=costs.get)
    assert cheapest in {"fake-small", "llama-3.1-8b-instruct"}
