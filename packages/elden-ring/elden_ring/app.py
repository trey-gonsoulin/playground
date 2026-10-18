"""FastAPI application factory."""

from fastapi import FastAPI
from mcp_lambda import create_mcp_app

from elden_ring.mcp_server import mcp


def create_app() -> FastAPI:
    return create_mcp_app(
        mcp,
        title="Elden Ring MCP",
        description="Self-hosted MCP server for Elden Ring build optimization and lore.",
    )


app = create_app()
