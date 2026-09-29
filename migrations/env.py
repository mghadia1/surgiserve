"""Alembic environment: run migrations against the configured database."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from surgiserve.models import Base
from surgiserve.settings import Settings

config = context.config
# Tests drive Alembic in-process and switch this off so it doesn't replace
# pytest's logging setup.
if config.config_file_name is not None and config.attributes.get(
    "configure_logging", True
):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or Settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (``alembic upgrade --sql``)."""

    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
