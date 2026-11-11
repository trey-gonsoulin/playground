"""FastAPI app: the MCP server at ``/mcp`` plus ``/healthz``, both behind a bearer token."""

import os
from typing import Any

from fastapi import FastAPI
from mcp_lambda import create_mcp_app

from vault_mcp import mcp_server
from vault_mcp.config import get_settings

TOKEN_ENV = "VAULT_MCP_TOKEN"  # noqa: S105 -- the variable name, not a secret


def create_app() -> FastAPI:
    # Fail closed: unlike the Lambda servers, this one never runs without a token.
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

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return mcp_server.health()

    return app
