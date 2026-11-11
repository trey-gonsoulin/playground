"""Entry point: ``vault-mcp`` listens on 127.0.0.1 only; Cloudflare Tunnel is the way in."""

import os

import uvicorn


def cli() -> None:
    uvicorn.run(
        "vault_mcp.app:create_app",
        factory=True,
        host=os.environ.get("VAULT_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("VAULT_MCP_PORT", "8765")),
    )


if __name__ == "__main__":
    cli()
