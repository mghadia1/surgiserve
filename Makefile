PYTHON ?= python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format typecheck services-up test migrate check weights up down reset smoke clean

# The frozen surgiseg-ml run whose weights the worker serves. The worker checks
# both files' SHA-256 on start and refuses to run anything else.
SURGISEG_RUN_DIR ?= ../surgiseg-ml/outputs/kvasir-frozen-final-2026-08-03

# The Compose services as published on the host, with a separate database
# and Redis database number for tests, so they never touch development data.
TEST_DATABASE_URL ?= postgresql+psycopg://surgiserve:surgiserve@localhost:5433/surgiserve_test
TEST_REDIS_URL ?= redis://localhost:6380/15

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

# CPU-only PyTorch first; from PyPI, x86 Linux would get the CUDA build.
install: $(BIN)/python
	$(BIN)/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0
	$(BIN)/python -m pip install -e '.[dev,model,bench]'

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy

services-up:
	docker compose up --detach --wait db redis

test: services-up
	SURGISERVE_TEST_DATABASE_URL=$(TEST_DATABASE_URL) \
	SURGISERVE_TEST_REDIS_URL=$(TEST_REDIS_URL) \
	$(BIN)/python -m pytest -q

# Apply migrations to the development database from the host.
migrate: services-up
	$(BIN)/alembic upgrade head

# Everything CI runs before the Docker job.
check: lint typecheck test

weights: weights/best-model.pt weights/config.json

weights/best-model.pt weights/config.json:
	mkdir -p weights
	cp $(SURGISEG_RUN_DIR)/$(notdir $@) $@

# --wait blocks until the container's HEALTHCHECK reports healthy.
up: weights
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
