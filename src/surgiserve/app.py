"""HTTP API.

Milestone 0 has one route. ``/health`` here is a liveness check only: it says
the process is up and serving requests. The deep check that also verifies
Postgres, Redis and the model arrives in Milestone 5.
"""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from . import __version__


class Health(BaseModel):
    status: Literal["ok"]
    version: str


def create_app() -> FastAPI:
    """Build the app. A factory, so each test gets a fresh instance."""

    app = FastAPI(
        title="SurgiServe",
        version=__version__,
        description=(
            "Educational instrument-segmentation service. Not a medical device. "
            "Do not upload real patient images."
        ),
    )

    @app.get("/health")
    def health() -> Health:
        return Health(status="ok", version=__version__)

    return app


app = create_app()
