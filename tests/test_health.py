import os
import time
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from conftest import alembic_config, ensure_database
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from surgiserve import __version__
from surgiserve.app import create_app
from surgiserve.health import SCHEMA_REVISION
from surgiserve.healthcheck import worker_is_alive
from surgiserve.job_queue import JobQueue
from surgiserve.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_health_reports_ok_and_version() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_liveness_does_not_depend_on_anything(settings: Settings) -> None:
    # Database and Redis both unreachable: liveness is still fine, because
    # restarting the API would not fix them.
    broken = settings.model_copy(
        update={
            "database_url": "postgresql+psycopg://x:x@127.0.0.1:1/x",
            "redis_url": "redis://127.0.0.1:1/0",
        }
    )

    assert TestClient(create_app(broken)).get("/health").status_code == 200


def test_unknown_route_is_404() -> None:
    client = TestClient(create_app())

    assert client.get("/does-not-exist").status_code == 404


def test_openapi_schema_lists_health() -> None:
    client = TestClient(create_app())

    schema = client.get("/openapi.json").json()

    assert "/health" in schema["paths"]
    assert "/health/ready" in schema["paths"]


def test_schema_revision_is_the_newest_migration() -> None:
    head = ScriptDirectory.from_config(alembic_config("unused")).get_current_head()

    assert SCHEMA_REVISION == head


def test_ready_when_every_dependency_is_up(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {name: check["ok"] for name, check in body["checks"].items()} == {
        "database": True,
        "schema": True,
        "redis": True,
        "storage": True,
    }
    assert body["checks"]["schema"]["detail"] == f"at {SCHEMA_REVISION}"
    assert body["workers"] == {"alive": 0, "models": {}}
    assert body["warnings"] == [
        "no live workers: jobs will be accepted but wait in the queue"
    ]
    assert body["queue"] == {
        "waiting": 0,
        "delayed": 0,
        "processing": 0,
        "dead_lettered": 0,
    }


def test_ready_reports_live_workers_their_models_and_queue_depths(
    client: TestClient, app: FastAPI
) -> None:
    queue: JobQueue = app.state.queue
    queue.beat("w1", 30, {"model": "surgiseg-resnet18-unet@5c2308207401"})
    queue.beat("w2", 30, {"model": "surgiseg-resnet18-unet@5c2308207401"})
    client.post(
        "/jobs",
        files={
            "image": (
                "f.png",
                (ROOT / "tests/fixtures/checkerboard-256.png").read_bytes(),
                "image/png",
            )
        },
    )

    body = client.get("/health/ready").json()

    assert body["workers"] == {
        "alive": 2,
        "models": {"surgiseg-resnet18-unet@5c2308207401": 2},
    }
    assert body["queue"]["waiting"] == 1
    assert body["warnings"] == []


def test_not_ready_when_redis_is_down(settings: Settings) -> None:
    broken = settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})

    response = TestClient(create_app(broken)).get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "fail"
    assert body["checks"]["redis"]["ok"] is False
    assert "ConnectionError" in body["checks"]["redis"]["detail"]
    assert body["checks"]["database"]["ok"] is True
    assert body["workers"] is None and body["queue"] is None


def test_not_ready_when_the_database_is_down(settings: Settings) -> None:
    broken = settings.model_copy(
        update={"database_url": "postgresql+psycopg://x:x@127.0.0.1:1/x"}
    )

    started = time.monotonic()
    response = TestClient(create_app(broken)).get("/health/ready")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert (checks["database"]["ok"], checks["schema"]["ok"]) == (False, False)
    assert checks["redis"]["ok"] is True
    assert response.json()["queue"]["dead_lettered"] is None
    assert time.monotonic() - started < 10  # connection timeouts, not a hang


def test_not_ready_when_the_schema_is_behind(settings: Settings) -> None:
    parsed = make_url(settings.database_url)
    url = parsed.set(database=f"{parsed.database}_behind").render_as_string(
        hide_password=False
    )
    ensure_database(url)
    config = alembic_config(url)
    command.downgrade(config, "base")
    command.upgrade(config, "0002")
    behind = settings.model_copy(update={"database_url": url})

    response = TestClient(create_app(behind)).get("/health/ready")

    assert response.status_code == 503
    schema = response.json()["checks"]["schema"]
    assert schema["ok"] is False
    assert f"database is at 0002, code needs {SCHEMA_REVISION}" in schema["detail"]


def test_not_ready_when_storage_is_not_writable(
    settings: Settings, tmp_path: Path
) -> None:
    not_a_directory = tmp_path / "a-file"
    not_a_directory.write_text("x")
    broken = settings.model_copy(update={"storage_dir": not_a_directory})

    response = TestClient(create_app(broken)).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["storage"]["ok"] is False


def test_worker_container_check_uses_the_liveness_file_age(tmp_path: Path) -> None:
    liveness = tmp_path / "alive"
    settings = Settings(
        worker_liveness_file=liveness, worker_liveness_max_age_seconds=60
    )

    assert worker_is_alive(settings) is False  # not started yet
    liveness.touch()
    assert worker_is_alive(settings) is True
    stale = time.time() - 120
    os.utime(liveness, (stale, stale))
    assert worker_is_alive(settings) is False
