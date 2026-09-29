# SurgiServe

An inference service for the instrument-segmentation model in
[surgiseg-ml](https://github.com/mghadia1/surgiseg-ml): upload a
gastrointestinal endoscopy frame, get back a mask of the instruments in it.

The point of the project is the service around the model: an API, a job
queue, a database, workers, failure handling and load testing.

> **What this is not:** a medical device or clinical tool. The model is an
> educational benchmark on one public research dataset (Kvasir-Instrument),
> which is licensed for research and educational use only.
> **Do not upload real patient images.**

## Status

| # | Milestone | Status |
|---|-----------|--------|
| 0 | Skeleton: tooling, CI, `/health`, Compose with only the API | Done |
| 1 | Postgres and Alembic, `POST /jobs`, `GET /jobs/{id}`, input validation | Not started |
| 2 | Redis queue and a worker running a fake model, end to end | Not started |
| 3 | Real inference with the surgiseg-ml checkpoint, masks and result links | Not started |
| 4 | Retries with backoff, timeouts, dead-letter queue, worker crash recovery | Not started |
| 5 | Structured JSON logs and a deep `/health` check | Not started |
| 6 | Locust load test with 1, 2 and 4 workers | Not started |
| 7 | Architecture diagram, design decisions, benchmarks | Not started |

Right now the service has one route, `GET /health`, a liveness check.

## Quickstart

Needs Docker.

```bash
docker compose up --build --detach --wait
curl http://localhost:8000/health
docker compose down
```

Interactive API docs are at <http://localhost:8000/docs> while it runs.

## Development

Needs Python 3.12.

```bash
make install   # create .venv and install with dev tools
make check     # ruff, mypy --strict, pytest
make smoke     # build the image, start it, curl /health, stop it
```

CI runs the same checks, then builds the image with Compose and calls
`/health` from outside the container.

## License

MIT. The model weights are not part of this repository.
