"""Provider HTTP behaviour with respx-mocked upstreams."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pulseroute_router.providers.anthropic import AnthropicProvider
from pulseroute_router.providers.openai import OpenAIProvider
from pulseroute_shared.types import ChatCompletionRequest, ChatMessage


def _req(model: str = "gpt-4o") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[
            ChatMessage(role="system", content="be terse"),
            ChatMessage(role="user", content="say hi"),
        ],
        max_tokens=32,
    )


@pytest.mark.asyncio
@respx.mock
async def test_openai_complete_happy_path():
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi back"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )
    )
    p = OpenAIProvider(api_key="sk-test")
    out = await p.complete(_req())
    assert out.content == "hi back"
    assert out.prompt_tokens == 5
    assert out.completion_tokens == 2
    assert out.finish_reason == "stop"
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_complete_5xx_raises():
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": "down"})
    )
    p = OpenAIProvider(api_key="sk-test")
    with pytest.raises(httpx.HTTPStatusError):
        await p.complete(_req())
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_stream_yields_deltas():
    body = (
        b'data: {"choices": [{"delta": {"content": "he"}}]}\n\n'
        b'data: {"choices": [{"delta": {"content": "llo"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )
    )
    p = OpenAIProvider(api_key="sk-test")
    deltas = [d async for d in p.stream(_req())]
    assert deltas == ["he", "llo"]
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_healthcheck_ok():
    respx.get("https://api.openai.com/v1/models").mock(return_value=httpx.Response(200, json={}))
    p = OpenAIProvider(api_key="sk-test")
    assert await p.healthcheck() is True
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_healthcheck_failure():
    respx.get("https://api.openai.com/v1/models").mock(side_effect=httpx.ConnectError("nope"))
    p = OpenAIProvider(api_key="sk-test")
    assert await p.healthcheck() is False
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_complete_happy_path():
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "model": "claude-3-5-sonnet",
                "content": [{"type": "text", "text": "hi back"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )
    )
    p = AnthropicProvider(api_key="sk-test")
    out = await p.complete(_req(model="claude-3-5-sonnet"))
    assert out.content == "hi back"
    assert out.prompt_tokens == 5
    assert out.completion_tokens == 2
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_payload_splits_system_correctly():
    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_capture)
    p = AnthropicProvider(api_key="sk-test")
    await p.complete(_req(model="claude-3-5-sonnet"))
    assert captured["body"]["system"] == "be terse"
    assert all(m["role"] != "system" for m in captured["body"]["messages"])
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_healthcheck_treats_401_as_reachable():
    respx.get("https://api.anthropic.com/v1/").mock(return_value=httpx.Response(401))
    p = AnthropicProvider(api_key="sk-test")
    assert await p.healthcheck() is True
    await p.aclose()
