from alembic import command
from conftest import alembic_config, ensure_database
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


def test_migrations_run_down_and_up_and_match_the_models(database_url: str) -> None:
    # A separate database, so going down to an empty schema cannot disturb the
    # one the API tests use.
    parsed = make_url(database_url)
    url = parsed.set(database=f"{parsed.database}_migrations").render_as_string(
        hide_password=False
    )
    ensure_database(url)
    config = alembic_config(url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(url)
    assert "jobs" not in inspect(engine).get_table_names()

    command.upgrade(config, "head")
    assert "jobs" in inspect(engine).get_table_names()
    engine.dispose()

    # Fails if the models describe a schema the migrations do not create.
    command.check(config)
