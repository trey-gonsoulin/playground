"""Local development entry point."""

import uvicorn


def cli() -> None:
    uvicorn.run(
        "ynab_mcp.app:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
    )


if __name__ == "__main__":
    cli()
