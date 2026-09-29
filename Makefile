PYTHON ?= python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format typecheck test check up down smoke clean

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

test:
	$(BIN)/python -m pytest -q

# Everything CI runs before the Docker job.
check: lint typecheck test

# --wait blocks until the container's HEALTHCHECK reports healthy.
up:
	docker compose up --build --detach --wait

down:
	docker compose down

# Bring the stack up, hit /health from outside the container, tear it down.
smoke: up
	curl --fail --silent --show-error http://localhost:8000/health; echo
	$(MAKE) down

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache build *.egg-info src/*.egg-info
