"""HTTP API. Health checks are in ``health.py``, job routes in ``jobs.py``."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .health import router as health_router
from .job_queue import JobQueue
from .jobs import router as jobs_router
from .jobs import too_large_detail
from .logs import RequestLogging
from .settings import Settings

# Room for the multipart boundaries and part headers around the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class ContentLengthLimit:
    """Refuse a request whose declared body is too large, before reading it.

    Without this, the multipart parser would spool the whole body to disk
    before the handler could look at its size. Clients that send no
    Content-Length (chunked uploads) get past this check; the handler's own
    size check still rejects them, after the body has been received.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, upload_limit: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.upload_limit = upload_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"content-length" and value.isdigit():
                    if int(value) > self.max_body_bytes:
                        response = JSONResponse(
                            {"detail": too_large_detail(self.upload_limit)},
                            status_code=413,
                        )
                        await response(scope, receive, send)
                        return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. A factory, so each test gets a fresh instance."""

    settings = settings or Settings()
    app = FastAPI(
        title="SurgiServe",
        version=__version__,
        description=(
            "Educational instrument-segmentation service. Not a medical device. "
            "Do not upload real patient images."
        ),
    )
    # create_engine does not connect; the first query does. So the app (and
    # /health) starts even while the database is still coming up. Short
    # timeouts, so a dependency that is down fails a request (or a readiness
    # check) in seconds instead of hanging it.
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
    # Like the engine, the Redis client connects on first use.
    app.state.redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )
    app.state.queue = JobQueue(app.state.redis)

    app.add_middleware(
        ContentLengthLimit,
        max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES,
        upload_limit=settings.max_upload_bytes,
    )
    # Added last, so it is outermost: requests refused by the size check
    # above are logged too.
    app.add_middleware(RequestLogging)

    app.include_router(health_router)
    app.include_router(jobs_router)
    return app


app = create_app()
