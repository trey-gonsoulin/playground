"""Shared FastAPI + Lambda wiring for the workspace's MCP servers (mcp 2.x, #504).

Every package's ``app.py`` is ``app = create_mcp_app(mcp, title=..., ...)``. The
helper fixes the transport settings API Gateway needs, mounts the server at
``/mcp`` and makes the lifespan re-enterable, which a warm Lambda container needs
because Mangum (``lifespan="auto"``) runs the whole lifespan on every invocation.
"""

import hmac
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["ReentrantMCP", "bearer_auth", "create_mcp_app", "normalize_mcp_path"]

Dispatch = Callable[
    [Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]
]


class ReentrantMCP:
    """ASGI app serving ``server`` over Streamable HTTP, rebuilt on each lifespan.

    ``StreamableHTTPSessionManager.run()`` is single-use (upstream won't change
    that, python-sdk#1692), so each ``lifespan()`` entry calls the public
    ``server.streamable_http_app()`` again, which builds a fresh session manager,
    and then enters its ``run()``. No private attributes are touched.

    The transport settings are fixed: ``json_response`` and ``stateless_http``
    (plain JSON, one request per invocation, as API Gateway needs), the server at
    the mount root, and a non-localhost ``host`` so the DNS-rebinding guard
    doesn't reject API Gateway's Host header with a 421. ``http_kwargs`` passes
    other ``streamable_http_app()`` settings, such as ``max_request_body_size``.
    """

    def __init__(self, server: MCPServer, **http_kwargs: Any) -> None:
        self.server = server
        self.http_kwargs: dict[str, Any] = {
            "streamable_http_path": "/",
            "json_response": True,
            "stateless_http": True,
            # Binds no socket; it only turns the DNS-rebinding guard off.
            "host": "0.0.0.0",  # noqa: S104 -- intentional, see above
        }
        fixed = self.http_kwargs.keys() & http_kwargs.keys()
        if fixed:
            raise TypeError(f"ReentrantMCP fixes {sorted(fixed)}; don't pass them")
        self.http_kwargs.update(http_kwargs)
        self._app: ASGIApp | None = None

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        app = self.server.streamable_http_app(**self.http_kwargs)
        async with self.server.session_manager.run():
            self._app = app
            try:
                yield
            finally:
                self._app = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            raise RuntimeError("ReentrantMCP called outside its lifespan")
        await self._app(scope, receive, send)


def bearer_auth(env_var: str) -> Dispatch:
    """Middleware requiring ``Authorization: Bearer $<env_var>`` when it is set.

    The variable is read per request; unset or empty means no auth.
    """

    async def check_auth(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        secret = os.environ.get(env_var)
        if secret:
            auth = request.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {secret}".encode()):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

    return check_auth


async def normalize_mcp_path(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Serve ``/mcp`` as ``/mcp/`` instead of letting Starlette redirect it."""
    if request.url.path == "/mcp":
        request.scope["path"] = "/mcp/"
    return await call_next(request)


def create_mcp_app(
    server: MCPServer,
    *,
    title: str,
    description: str,
    version: str = "0.1.0",
    auth_env: str | None = None,
    **http_kwargs: Any,
) -> FastAPI:
    """FastAPI app with ``server`` at ``/mcp``, optionally behind ``bearer_auth``.

    ``http_kwargs`` go to ``ReentrantMCP`` (e.g. ``max_request_body_size``).
    """
    mcp_app = ReentrantMCP(server, **http_kwargs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp_app.lifespan():
            yield

    app = FastAPI(
        title=title, description=description, version=version, lifespan=lifespan
    )
    # Middleware is applied outermost-last, so auth (added last) runs first.
    app.add_middleware(BaseHTTPMiddleware, dispatch=normalize_mcp_path)
    if auth_env:
        app.add_middleware(BaseHTTPMiddleware, dispatch=bearer_auth(auth_env))
    app.mount("/mcp", mcp_app)
    return app
