PYTHON ?= python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format typecheck db-up test migrate check up down reset smoke clean

# The Compose database as published on the host, with a separate database for
# tests so they never touch development data.
TEST_DATABASE_URL ?= postgresql+psycopg://surgiserve:surgiserve@localhost:5433/surgiserve_test

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

install: $(BIN)/python
	$(BIN)/python -m pip install -e '.[dev]'

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy

db-up:
	docker compose up --detach --wait db

test: db-up
	SURGISERVE_TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/python -m pytest -q

# Apply migrations to the development database from the host.
migrate: db-up
	$(BIN)/alembic upgrade head

# Everything CI runs before the Docker job.
check: lint typecheck test

# --wait blocks until the container's HEALTHCHECK reports healthy.
up:
	docker compose up --build --detach --wait

down:
	docker compose down

# Also delete the database and storage volumes.
reset:
	docker compose down --volumes

# Bring the stack up, exercise it over HTTP from outside, tear it down.
smoke: up
	./scripts/smoke.sh
	$(MAKE) down

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache build *.egg-info src/*.egg-info
