"""Entry point on the vault box: ``vault-mcp``, HTTPS with the box's own CA.

The Lambda proxy reaches it on the instance's public DNS name (port 8443), so
TLS and the bearer token are what protect it; see deploy/server/install.sh.
"""

import os

import uvicorn


def cli() -> None:
    cert = os.environ.get("VAULT_MCP_SSL_CERT")
    uvicorn.run(
        "vault_mcp.app:create_app",
        factory=True,
        host=os.environ.get("VAULT_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("VAULT_MCP_PORT", "8765")),
        ssl_certfile=cert,
        ssl_keyfile=os.environ.get("VAULT_MCP_SSL_KEY") if cert else None,
    )


if __name__ == "__main__":
    cli()
