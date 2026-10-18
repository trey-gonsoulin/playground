"""FastAPI application factory."""

from fastapi import FastAPI
from mcp_lambda import create_mcp_app

from genealogy.mcp_server import MAX_REQUEST_BODY_BYTES, mcp


def create_app() -> FastAPI:
    # Requires Authorization: Bearer <GENEALOGY_MCP_SECRET> when the env var is set.
    return create_mcp_app(
        mcp,
        title="Genealogy MCP",
        description="MCP server for building a personal family tree.",
        auth_env="GENEALOGY_MCP_SECRET",
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
    )


app = create_app()
