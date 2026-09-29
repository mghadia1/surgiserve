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
| 1 | Postgres and Alembic, `POST /jobs`, `GET /jobs/{id}`, input validation | Built; waiting on CI |
| 2 | Redis queue and a worker running a fake model, end to end | Built; waiting on CI |
| 3 | Real inference with the surgiseg-ml checkpoint, masks and result links | Not started |
| 4 | Retries with backoff, timeouts, dead-letter queue, worker crash recovery | Not started |
| 5 | Structured JSON logs and a deep `/health` check | Not started |
| 6 | Locust load test with 1, 2 and 4 workers | Not started |
| 7 | Architecture diagram, design decisions, benchmarks | Not started |

Right now uploads go through the whole pipeline, but the worker runs a
**fake model** (`fake-brightness-threshold`: bright pixels become "instrument").
Its masks mean nothing about instruments. The real network arrives in
Milestone 3.

## How a job flows

1. `POST /jobs` validates and stores the image, inserts a `queued` row in
   Postgres, then pushes the job id onto a Redis list.
2. A worker claims the id with `BLMOVE`, which atomically moves it from the
   queue to that worker's own processing list.
3. The worker flips the row from `queued` to `running` with a conditional
   `UPDATE … WHERE status = 'queued'`. If no row changes (the job is already
   done, or the id was delivered twice) it skips the job.
4. It runs the model, writes the mask to `masks/<job id>.png`, and marks the
   job `succeeded`, or `failed` with the error if the model or the job's own
   data was the problem.
5. Only then does it remove the id from its processing list.

The queue carries only ids; Postgres is the record of each job. The queue is
about 60 lines in `src/surgiserve/job_queue.py`, written by hand rather than
using Celery or RQ, so every step, and later every failure path, is visible.

What happens when things break, today:

| Failure | Result |
|---------|--------|
| Redis down when a job is submitted | Row and file removed, `503`, client retries |
| Model raises, or the input file is missing | Job `failed` with the error; not retried yet |
| Database or Redis down while a worker holds a job | Id stays on the worker's processing list; the job stays `queued` |
| Worker killed mid-job | Id stays on its processing list; the job stays `running` |
| Worker sent SIGTERM (`docker compose stop`) | Finishes the current job, then exits |

The ids held on processing lists are not recovered yet, and a job whose id
never reached Redis stays `queued`. Recovery, retries, timeouts and a
dead-letter queue are Milestone 4.

## API

| Route | What it does |
|-------|--------------|
| `GET /health` | Liveness: the process is up. Does not touch the database or Redis. |
| `POST /jobs` | Multipart upload, field `image`. Returns `202` with the job and a `Location` header. |
| `GET /jobs/{id}` | The job's status, what was uploaded, and how it went. |

```bash
curl -F "image=@tests/fixtures/checkerboard-256.png" http://localhost:8000/jobs
```

A moment later, `GET /jobs/{id}`:

```json
{"id": "3c945986-…", "status": "succeeded",
 "created_at": "…", "updated_at": "…", "started_at": "…", "finished_at": "…",
 "model": "fake-brightness-threshold", "error": null,
 "input": {"filename": "checkerboard-256.png", "format": "PNG",
           "width": 256, "height": 256, "size_bytes": 647, "sha256": "e390…"}}
```

The mask is on the storage volume; an endpoint to download it arrives in
Milestone 3.

### What an upload must be

| Rule | Otherwise |
|------|-----------|
| A PNG or JPEG, judged by decoding the bytes (the file name and Content-Type are ignored) | `415` |
| Not empty, not truncated or corrupt (every pixel is decoded on upload) | `422` |
| Each side between 32 and 4096 pixels (checked from the header, before decoding) | `422` |
| At most 10 MiB | `413` |

A rejected upload leaves no job row and no file. Limits are set with
`SURGISERVE_MAX_UPLOAD_BYTES`, `SURGISERVE_MIN_IMAGE_SIDE` and
`SURGISERVE_MAX_IMAGE_SIDE`.

### How an upload is stored

- The file is saved under the storage volume as `inputs/<job id>.<png|jpg>`.
  The client's file name is kept for display only and never used in a path.
- It is written to a temporary file, flushed, then renamed into place, so a
  crash never leaves a half-written image under a real name.
- File first, then the database row. If the insert fails, the file is deleted.
  If the process dies in between, an unreferenced file is left behind, which
  is harmless; the reverse order could leave a job whose image is missing.
- Oversized uploads that declare a `Content-Length` are refused before the
  body is read. Chunked uploads without one are received first and then
  refused by the handler. A reverse proxy in front would be the place for a
  hard body limit.

## Quickstart

Needs Docker.

```bash
docker compose up --build --detach --wait
curl http://localhost:8000/health
docker compose down            # keeps the database and uploads
docker compose down --volumes  # deletes them
```

Compose starts Postgres and Redis, then a one-shot `migrate` service that
runs `alembic upgrade head` and exits, then the API and one worker, which both
wait for the migration to succeed. Postgres is published on `127.0.0.1:5433`
and Redis on `127.0.0.1:6380`, with development-only credentials. Redis keeps
an append-only file, so queued ids survive a Redis restart.

More workers:

```bash
docker compose up --detach --wait --scale worker=3
```

Interactive API docs are at <http://localhost:8000/docs> while it runs.

## Development

Needs Python 3.12.

```bash
make install   # create .venv and install with dev tools
make check     # ruff, mypy --strict, pytest (starts the Compose database)
make smoke     # start the stack, upload an image over HTTP, stop it
```

The service tests use their own `surgiserve_test` database, which they create
and migrate themselves, and Redis database 15, emptied before each test.
Without `SURGISERVE_TEST_DATABASE_URL` and `SURGISERVE_TEST_REDIS_URL` they
skip locally and fail in CI. A migration test runs the schema down to empty and
back up, then checks that the migrations match the models.

CI runs the same checks against Postgres and Redis services, then brings the
whole stack up with Compose and runs `scripts/smoke.sh`, which uploads an
image and waits for the worker to finish it.

## License

MIT. The model weights are not part of this repository.
