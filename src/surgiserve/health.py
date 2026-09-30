"""Health checks.

``GET /health`` is liveness: the process is up. It touches nothing else, on
purpose: Docker restarts a container whose liveness check fails, and
restarting the API would not fix a database outage.

``GET /health/ready`` is readiness: can this instance do its job right now?
It checks each dependency with a short timeout and answers 503 if any fails:

- ``database``: Postgres answers a query.
- ``schema``: the database is migrated to the revision this code expects. An
  API on an old schema would fail on its first insert.
- ``redis``: Redis answers PING.
- ``storage``: a file can be written to the storage directory and removed.

It also reports, without affecting the status code, the live workers and the
model each one runs (from their heartbeats), queue depths and the number of
dead-lettered jobs. No live worker is a warning, not a failure: the API can
still accept jobs, which will wait in the queue.
"""

import time
import uuid
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select, text

from . import __version__
from .job_queue import JobQueue
from .models import Job, JobStatus
from .settings import Settings

# The Alembic revision this code needs. tests/test_health.py checks it is the
# newest migration, so adding one without updating this fails the tests.
SCHEMA_REVISION = "0003"

router = APIRouter()


class Liveness(BaseModel):
    status: Literal["ok"]
    version: str


class Check(BaseModel):
    ok: bool
    ms: float
    detail: str | None = None


class Workers(BaseModel):
    alive: int
    # How many live workers run each model, e.g. {"surgiseg-...@5c2308207401": 2}.
    models: dict[str, int]


class QueueDepths(BaseModel):
    waiting: int
    delayed: int
    processing: int
    dead_lettered: int | None


class Readiness(BaseModel):
    status: Literal["ok", "fail"]
    version: str
    checks: dict[str, Check]
    workers: Workers | None
    queue: QueueDepths | None
    warnings: list[str]


def run_check(probe: Callable[[], str | None]) -> Check:
    """Time a probe; any exception fails the check with a short reason."""

    started = time.perf_counter()
    try:
        detail = probe()
        ok = True
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"[:300]
        ok = False
    return Check(
        ok=ok, ms=round((time.perf_counter() - started) * 1000, 1), detail=detail
    )


@router.get("/health")
def health() -> Liveness:
    return Liveness(status="ok", version=__version__)


@router.get(
    "/health/ready",
    responses={503: {"model": Readiness, "description": "A dependency is failing"}},
)
def ready(request: Request, response: Response) -> Readiness:
    settings: Settings = request.app.state.settings
    queue: JobQueue = request.app.state.queue
    engine = request.app.state.engine

    def database() -> None:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def schema() -> str:
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if revision != SCHEMA_REVISION:
            raise RuntimeError(
                f"database is at {revision}, code needs {SCHEMA_REVISION}"
            )
        return f"at {revision}"

    def redis() -> None:
        queue.redis.ping()

    def storage() -> None:
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.storage_dir / f".health-{uuid.uuid4().hex}"
        probe.write_bytes(b"ok")
        probe.unlink()

    checks = {
        "database": run_check(database),
        "schema": run_check(schema),
        "redis": run_check(redis),
        "storage": run_check(storage),
    }

    warnings: list[str] = []
    workers = queue_depths = None
    if checks["redis"].ok:
        live = queue.live_workers()
        models: dict[str, int] = {}
        for info in live.values():
            name = str(info.get("model", "unknown"))
            models[name] = models.get(name, 0) + 1
        workers = Workers(alive=len(live), models=models)
        if not live:
            warnings.append(
                "no live workers: jobs will be accepted but wait in the queue"
            )
        dead_lettered = None
        if checks["database"].ok:
            with engine.connect() as connection:
                dead_lettered = connection.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.status == JobStatus.DEAD_LETTERED)
                )
        queue_depths = QueueDepths(**queue.depths(), dead_lettered=dead_lettered)

    ok = all(check.ok for check in checks.values())
    if not ok:
        response.status_code = 503
    return Readiness(
        status="ok" if ok else "fail",
        version=__version__,
        checks=checks,
        workers=workers,
        queue=queue_depths,
        warnings=warnings,
    )
