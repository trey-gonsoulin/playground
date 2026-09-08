"""AWS Lambda entry point — wraps the FastAPI app with Mangum."""

from mangum import Mangum

from ynab_mcp.app import app

handler = Mangum(app, lifespan="auto")
