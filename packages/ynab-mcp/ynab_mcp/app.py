"""FastAPI application factory."""

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from ynab_mcp.mcp_server import mcp


async def _check_auth(request: Request, call_next):
    """Require Authorization: Bearer <YNAB_MCP_SECRET> when the env var is set."""
    secret = os.environ.get("YNAB_MCP_SECRET")
    if secret:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {secret}":
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


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
        title="YNAB MCP",
        description="Self-hosted MCP server for YNAB budget management.",
        version="0.1.0",
        lifespan=_lifespan,
    )
    # Middleware is applied outermost-last, so auth (added last) runs first.
    app.add_middleware(BaseHTTPMiddleware, dispatch=_normalize_mcp_path)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_check_auth)
    app.mount("/mcp", mcp.streamable_http_app())
    return app


app = create_app()
