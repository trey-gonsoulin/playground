"""FastAPI application factory."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from elden_ring.mcp_server import mcp


async def _normalize_mcp_path(request: Request, call_next):
    if request.url.path == "/mcp":
        request.scope["path"] = "/mcp/"
    return await call_next(request)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    sm = mcp.session_manager
    if sm._has_started and sm._task_group is None:
        sm._has_started = False
    async with sm.run():
        yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Elden Ring MCP",
        description="Self-hosted MCP server for Elden Ring build optimization and lore.",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=_normalize_mcp_path)
    app.mount("/mcp", mcp.streamable_http_app())
    return app


app = create_app()
