"""Local development entry point."""

import uvicorn


def cli() -> None:
    uvicorn.run(
        "markets.app:app",
        host="0.0.0.0",
        port=8003,
        reload=True,
    )


if __name__ == "__main__":
    cli()
