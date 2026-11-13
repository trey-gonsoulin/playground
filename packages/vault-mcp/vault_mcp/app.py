"""FastAPI app on the vault box: the MCP server at ``/mcp`` plus ``/healthz`` and
``/readyz``, all behind a bearer token. The Lambda proxy (proxy.py) calls it."""

import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from mcp_lambda import create_mcp_app, is_authorized
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from vault_mcp import _ready, mcp_server
from vault_mcp.config import get_settings

TOKEN_ENV = "VAULT_MCP_TOKEN"  # noqa: S105 -- the variable name, not a secret


def _touch_activity() -> None:
    path = get_settings().activity_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


async def _record_activity(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Touch the activity file on authenticated MCP requests; the idle shutdown
    reads its mtime. (Port 8443 is public, so scanners' requests must not count.)

    Touched on arrival as well as on completion, so vault-idle-stop sees a call
    that is still running and backs off instead of powering off under it.
    """
    if not request.url.path.startswith("/mcp"):
        return await call_next(request)
    if is_authorized(request, TOKEN_ENV):
        _touch_activity()
    response = await call_next(request)
    if response.status_code != 401:
        _touch_activity()
    return response


def create_app() -> FastAPI:
    # Fail closed: this server is never meant to run without a token.
    # VAULT_MCP_ALLOW_NO_AUTH=1 is for local experiments only.
    if (
        not os.environ.get(TOKEN_ENV)
        and os.environ.get("VAULT_MCP_ALLOW_NO_AUTH") != "1"
    ):
        raise RuntimeError(
            f"{TOKEN_ENV} is not set; refusing to serve the vault without auth"
        )
    server = mcp_server.create_server(readonly=get_settings().readonly)
    app = create_mcp_app(
        server,
        title="Obsidian Vault MCP",
        description="Search, read and edit an Obsidian vault through the Obsidian CLI.",
        auth_env=TOKEN_ENV,
    )
    # Added last, so it is outermost and sees the auth middleware's 401s.
    app.add_middleware(BaseHTTPMiddleware, dispatch=_record_activity)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return mcp_server.health()

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        s = get_settings()
        st = _ready.state(s)  # always fresh: vault-idle-stop needs "settled"
        return {**st, "latched": _ready.check(s, st)[0]}

    return app
