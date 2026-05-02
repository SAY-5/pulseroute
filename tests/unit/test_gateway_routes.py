"""End-to-end gateway tests using FastAPI's ASGI test transport + FakeProvider."""

from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_chat_completions_unauthorized(app_client):
    r = await app_client.post(
        "/v1/chat/completions",
        json={"model": "fake-large", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_chat_completions_happy_path(app_client):
    r = await app_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer pr_test_quality"},
        json={
            "model": "fake-large",
            "messages": [{"role": "user", "content": "hello world"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert len(body["choices"]) == 1
    assert "fake[" in body["choices"][0]["message"]["content"]
    assert "pulseroute" in body
    assert body["pulseroute"]["cache_hit"] is False


@pytest.mark.asyncio
async def test_chat_completions_cache_hit_on_repeat(app_client):
    payload = {
        "model": "fake-large",
        "messages": [{"role": "user", "content": "what is the capital of France?"}],
    }
    headers = {"Authorization": "Bearer pr_test_quality"}
    first = await app_client.post("/v1/chat/completions", headers=headers, json=payload)
    assert first.status_code == 200
    assert first.json()["pulseroute"]["cache_hit"] is False
    second = await app_client.post("/v1/chat/completions", headers=headers, json=payload)
    assert second.status_code == 200
    assert second.json()["pulseroute"]["cache_hit"] is True


@pytest.mark.asyncio
async def test_streaming_emits_sse_chunks(app_client):
    headers = {"Authorization": "Bearer pr_test_quality"}
    payload = {
        "model": "fake-large",
        "messages": [{"role": "user", "content": "stream please"}],
        "stream": True,
        "pulseroute_no_cache": True,
    }
    async with app_client.stream(
        "POST", "/v1/chat/completions", headers=headers, json=payload
    ) as resp:
        assert resp.status_code == 200
        chunks = []
        async for line in resp.aiter_lines():
            if line.startswith("data: ") and "[DONE]" not in line:
                chunks.append(json.loads(line.removeprefix("data: ")))
            elif "[DONE]" in line:
                break
    assert chunks
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_admin_policies_list(app_client):
    r = await app_client.get("/v1/admin/policies")
    assert r.status_code == 200
    body = r.json()
    assert "data" in body


@pytest.mark.asyncio
async def test_admin_create_api_key_returns_plaintext_once(app_client):
    r = await app_client.post(
        "/v1/admin/api-keys", json={"tenant_id": "tenant_quality", "label": "ci"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("pr_live_")
    assert body["sha256_hash"]
    assert body["prefix"] == body["key"][:10]


@pytest.mark.asyncio
async def test_admin_requests_pagination(app_client):
    r = await app_client.get("/v1/admin/requests", params={"limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 5
    assert body["next_cursor"] is not None


@pytest.mark.asyncio
async def test_healthz(app_client):
    r = await app_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_metrics_exposed(app_client):
    r = await app_client.get("/metrics")
    assert r.status_code == 200
    assert b"gateway_added_latency_seconds" in r.content
