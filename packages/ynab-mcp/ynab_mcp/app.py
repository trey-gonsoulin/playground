"""FastAPI application factory."""

from fastapi import FastAPI
from mcp_lambda import create_mcp_app

from ynab_mcp.mcp_server import mcp


def create_app() -> FastAPI:
    # Requires Authorization: Bearer <YNAB_MCP_SECRET> when the env var is set.
    return create_mcp_app(
        mcp,
        title="YNAB MCP",
        description="Self-hosted MCP server for YNAB budget management.",
        auth_env="YNAB_MCP_SECRET",
    )


app = create_app()
