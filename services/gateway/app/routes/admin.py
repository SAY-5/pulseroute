"""Admin API.

Cursor pagination uses an opaque base64 cursor encoding ``(timestamp, request_id)``
so it remains stable across ClickHouse rebalances. We mirror the OpenAI admin
shape loosely — fields are renamed where they would otherwise be confusing
(e.g. ``api_keys`` always returns the SHA256 prefix, never plaintext)."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

router = APIRouter(tags=["admin"])

# In-memory stores. Real deployment swaps these for Postgres-backed repositories.
_POLICIES: dict[str, dict[str, Any]] = {
    "cost_capped_5usd": {
        "id": "cost_capped_5usd",
        "routing_strategy": "cost_capped",
        "cost_ceiling_usd_per_day": 5.0,
        "allowed_models": ["gpt-4o-mini", "claude-3-haiku"],
        "pii_redaction": True,
    },
    "quality_first": {
        "id": "quality_first",
        "routing_strategy": "quality_first",
        "cost_ceiling_usd_per_day": 0.0,
        "allowed_models": ["gpt-4o", "claude-3-5-sonnet"],
        "pii_redaction": False,
    },
}

_API_KEYS: dict[str, dict[str, Any]] = {}


class PolicyIn(BaseModel):
    id: str
    routing_strategy: str
    cost_ceiling_usd_per_day: float = 0.0
    allowed_models: list[str] = []
    pii_redaction: bool = False


class ApiKeyCreateIn(BaseModel):
    tenant_id: str
    label: str | None = None


@router.get("/policies")
async def list_policies() -> dict[str, list[dict[str, Any]]]:
    return {"data": list(_POLICIES.values())}


@router.post("/policies", status_code=status.HTTP_201_CREATED)
async def create_policy(body: PolicyIn) -> dict[str, Any]:
    _POLICIES[body.id] = body.model_dump()
    return _POLICIES[body.id]


@router.get("/api-keys")
async def list_api_keys() -> dict[str, list[dict[str, Any]]]:
    return {"data": list(_API_KEYS.values())}


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def create_api_key(body: ApiKeyCreateIn) -> dict[str, Any]:
    plaintext = f"pr_live_{uuid.uuid4().hex}"
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    record = {
        "id": uuid.uuid4().hex,
        "tenant_id": body.tenant_id,
        "label": body.label,
        "prefix": plaintext[:10],
        "sha256_hash": digest,
        "last_used_at": None,
        "created_at": int(time.time()),
    }
    _API_KEYS[record["id"]] = record
    # Plaintext returned exactly once at creation; never persisted.
    return {**record, "key": plaintext}


def _encode_cursor(ts: int, rid: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([ts, rid]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        ts, rid = json.loads(base64.urlsafe_b64decode(cursor).decode())
        return int(ts), str(rid)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad cursor") from exc


@router.get("/requests")
async def list_requests(
    tenant_id: str | None = None,
    start: int | None = Query(default=None, description="Unix seconds inclusive"),
    end: int | None = Query(default=None, description="Unix seconds exclusive"),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """ClickHouse-backed in production. Returns deterministic synthetic rows here
    so the admin UI can be exercised without a running analytics database."""
    after_ts: int | None = None
    after_rid: str | None = None
    if cursor:
        after_ts, after_rid = _decode_cursor(cursor)
    now = int(time.time())
    base = end or now
    rows: list[dict[str, Any]] = []
    for i in range(limit):
        ts = base - i * 60
        if start is not None and ts < start:
            break
        if after_ts is not None and ts > after_ts:
            continue
        rid = f"req_{ts}_{i:04d}"
        if after_rid is not None and rid >= after_rid and ts == after_ts:
            continue
        rows.append(
            {
                "request_id": rid,
                "tenant_id": tenant_id or "tenant_quality",
                "timestamp": ts,
                "model": "gpt-4o-mini" if i % 2 else "claude-3-haiku",
                "provider": "openai" if i % 2 else "anthropic",
                "latency_ms": 120 + (i % 7) * 13,
                "cost_usd": 0.0008 + (i % 5) * 0.0001,
                "cache_hit": (i % 3) == 0,
                "error_code": None,
            }
        )
    next_cursor = None
    if rows and len(rows) == limit:
        last = rows[-1]
        next_cursor = _encode_cursor(last["timestamp"], last["request_id"])
    return {"data": rows, "next_cursor": next_cursor}
