"""FastAPI application factory."""

from fastapi import FastAPI
from mcp_lambda import create_mcp_app

from markets.mcp_server import mcp


def create_app() -> FastAPI:
    # Requires Authorization: Bearer <MARKETS_MCP_SECRET> when the env var is set.
    return create_mcp_app(
        mcp,
        title="Markets MCP",
        description=(
            "Short-horizon equity analysis from Yahoo Finance: earnings event studies, "
            "implied and forecast volatility, price-level odds and sell-plan simulation."
        ),
        auth_env="MARKETS_MCP_SECRET",
    )


app = create_app()
