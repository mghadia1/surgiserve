import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from surgiserve.app import create_app
from surgiserve.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL_ENV = "SURGISERVE_TEST_DATABASE_URL"


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
    """A migrated, dedicated test database. Tests that need it skip without it,
    except in CI, where a missing database is a configuration error."""

    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        if os.environ.get("CI"):
            pytest.fail(f"{TEST_DATABASE_URL_ENV} must be set in CI")
        pytest.skip(f"{TEST_DATABASE_URL_ENV} not set; `make test` starts a database")
    ensure_database(url)
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture
def settings(tmp_path: Path, database_url: str) -> Settings:
    return Settings(database_url=database_url, storage_dir=tmp_path / "storage")


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    app = create_app(settings)
    with app.state.engine.begin() as connection:
        connection.execute(text("TRUNCATE jobs"))
    yield app
    app.state.engine.dispose()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)
