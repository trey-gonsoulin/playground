"""Local development entry point."""

import uvicorn


def cli() -> None:
    uvicorn.run(
        "genealogy.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    cli()
