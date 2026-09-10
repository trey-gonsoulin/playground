"""Local development entry point."""

import uvicorn


def cli() -> None:
    uvicorn.run(
        "elden_ring.app:app",
        host="0.0.0.0",
        port=8002,
        reload=True,
    )


if __name__ == "__main__":
    cli()
