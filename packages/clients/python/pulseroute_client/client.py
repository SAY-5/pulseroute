"""Tiny client. We deliberately do not depend on the openai SDK so users can
swap us in without pulling extra deps.

Usage:
    from pulseroute_client import OpenAI
    c = OpenAI(api_key="pr_test_quality", base_url="http://localhost:8080/v1")
    r = c.chat.completions.create(model="gpt-4o-mini",
                                  messages=[{"role": "user", "content": "hi"}])
"""

from __future__ import annotations

from typing import Any

import httpx


class _ChatCompletions:
    def __init__(self, parent: PulseRouteClient) -> None:
        self._p = parent

    def create(self, **kwargs: Any) -> dict[str, Any]:
        resp = self._p._http.post("/chat/completions", json=kwargs)
        resp.raise_for_status()
        return resp.json()


class _Chat:
    def __init__(self, parent: PulseRouteClient) -> None:
        self.completions = _ChatCompletions(parent)


class PulseRouteClient:
    def __init__(self, api_key: str, base_url: str = "http://localhost:8080/v1") -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )
        self.chat = _Chat(self)

    def close(self) -> None:
        self._http.close()


# Alias so existing OpenAI-SDK imports work as a drop-in.
OpenAI = PulseRouteClient
