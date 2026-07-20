"""AWS Lambda entry point — wraps the FastAPI app with Mangum."""

from mangum import Mangum

from therapist_finder.app import app

handler = Mangum(app, lifespan="auto")
