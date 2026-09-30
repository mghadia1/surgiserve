import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from surgiserve.app import create_app
from surgiserve.logs import install_context_factory
from surgiserve.settings import Settings

# Log records carry context fields (request_id, job_id, ...) as attributes, so
# tests can read them from caplog as the JSON formatter would.
install_context_factory()

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL_ENV = "SURGISERVE_TEST_DATABASE_URL"
TEST_REDIS_URL_ENV = "SURGISERVE_TEST_REDIS_URL"


def required_service_url(variable: str, how: str) -> str:
    """Read a test service URL. Tests that need it skip without it, except in
    CI, where a missing service is a configuration error."""

    url = os.environ.get(variable)
    if not url:
        if os.environ.get("CI"):
            pytest.fail(f"{variable} must be set in CI")
        pytest.skip(f"{variable} not set; {how}")
    return url


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logging"] = False
    return config


def ensure_database(url: str) -> None:
    """Create the database named in ``url`` if it does not exist yet."""

    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": parsed.database},
        )
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    """A migrated, dedicated test database."""

    url = required_service_url(TEST_DATABASE_URL_ENV, "`make test` starts a database")
    ensure_database(url)
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture(scope="session")
def redis_url() -> str:
    """A Redis database number set aside for tests; emptied before each test."""

    return required_service_url(TEST_REDIS_URL_ENV, "`make test` starts Redis")


@pytest.fixture
def settings(tmp_path: Path, database_url: str, redis_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        redis_url=redis_url,
        storage_dir=tmp_path / "storage",
    )


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    app = create_app(settings)
    with app.state.engine.begin() as connection:
        connection.execute(text("TRUNCATE jobs"))
    redis: Redis = app.state.redis
    redis.flushdb()
    yield app
    redis.close()
    app.state.engine.dispose()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)
