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
| 2 | Redis queue and a worker running a fake model, end to end | Not started |
| 3 | Real inference with the surgiseg-ml checkpoint, masks and result links | Not started |
| 4 | Retries with backoff, timeouts, dead-letter queue, worker crash recovery | Not started |
| 5 | Structured JSON logs and a deep `/health` check | Not started |
| 6 | Locust load test with 1, 2 and 4 workers | Not started |
| 7 | Architecture diagram, design decisions, benchmarks | Not started |

Right now the service accepts and stores uploads, but **nothing processes
them yet**: every job stays `queued` until the worker arrives in Milestone 2.

## API

| Route | What it does |
|-------|--------------|
| `GET /health` | Liveness: the process is up. Does not touch the database. |
| `POST /jobs` | Multipart upload, field `image`. Returns `202` with the job and a `Location` header. |
| `GET /jobs/{id}` | The job's status and what was uploaded. |

```bash
curl -F "image=@tests/fixtures/checkerboard-256.png" http://localhost:8000/jobs
```

```json
{"id": "9136e4f5-…", "status": "queued",
 "created_at": "…", "updated_at": "…",
 "input": {"filename": "checkerboard-256.png", "format": "PNG",
           "width": 256, "height": 256, "size_bytes": 647, "sha256": "e390…"}}
```

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

Compose starts Postgres, then a one-shot `migrate` service that runs
`alembic upgrade head` and exits, then the API, which waits for the migration
to succeed. Postgres is published on `127.0.0.1:5433` with development-only
credentials.

Interactive API docs are at <http://localhost:8000/docs> while it runs.

## Development

Needs Python 3.12.

```bash
make install   # create .venv and install with dev tools
make check     # ruff, mypy --strict, pytest (starts the Compose database)
make smoke     # start the stack, upload an image over HTTP, stop it
```

The database tests use their own `surgiserve_test` database, which they create
and migrate themselves. Without `SURGISERVE_TEST_DATABASE_URL` they skip
locally and fail in CI. A migration test runs the schema down to empty and
back up, then checks that the migrations match the models.

CI runs the same checks against a Postgres service, then brings the whole
stack up with Compose and runs `scripts/smoke.sh` against it.

## License

MIT. The model weights are not part of this repository.
