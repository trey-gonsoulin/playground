"""FastAPI application factory."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from therapist_finder.mcp_server import mcp


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
        title="Therapist Finder",
        description="MCP server for searching therapist directories.",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=_normalize_mcp_path)
    app.add_exception_handler(ValueError, _unprocessable)
    app.mount("/mcp", mcp.streamable_http_app())
    return app


def _unprocessable(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


app = create_app()
