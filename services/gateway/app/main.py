"""FastAPI app factory and uvicorn entrypoint for the gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pulseroute_gateway.deps import get_dependencies
from pulseroute_gateway.metrics import hdr_exposition
from pulseroute_gateway.routes import admin, chat
from pulseroute_shared.otel import configure_logging, init_tracing
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_tracing("pulseroute-gateway")
    configure_logging()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="PulseRoute",
        version="0.1.0",
        description="OpenAI-compatible LLM gateway with policy-driven routing.",
        lifespan=lifespan,
    )
    app.include_router(chat.router, prefix="/v1")
    app.include_router(admin.router, prefix="/v1/admin")
    app.add_middleware(RequestIdMiddleware)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        body = generate_latest() + hdr_exposition().encode()
        return Response(body, media_type=CONTENT_TYPE_LATEST)

    # Force a single shared dependency container per process.
    app.state.deps = get_dependencies()
    return app


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        import uuid

        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response


app = create_app()
