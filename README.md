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
| 3 | Real inference with the surgiseg-ml checkpoint, masks and result links | Built; waiting on CI |
| 4 | Retries with backoff, timeouts, dead-letter queue, worker crash recovery | Built; waiting on CI |
| 5 | Structured JSON logs and a deep `/health` check | Built; waiting on CI |
| 6 | Locust load test with 1, 2 and 4 workers | Not started |
| 7 | Architecture diagram, design decisions, benchmarks | Not started |

The worker runs the real network: the frozen ResNet-18 U-Net from
surgiseg-ml (run of 2026-08-03, Dice 0.9226 on Kvasir-Instrument's 118-image
official test split, scored at 128x128). A fake brightness-threshold model
remains for running without weights.

## The model

- **Code:** the network and its config class are imported from surgiseg-ml,
  installed at a pinned commit, not copied. `src/surgiserve/surgiseg_model.py`
  adds only the inference path around it: RGB / 255, stretch to 128x128
  (bilinear), sigmoid, average with the horizontally flipped prediction,
  resize the probabilities back to the upload's size, threshold at `>= 0.47`.
- **Weights:** the frozen checkpoint (57 MB) is not in this repository. The
  worker reads it from `weights/`, mounted read-only, and refuses to start
  unless the checkpoint and its config hash to the SHA-256 values recorded by
  the benchmark.
- **Reproduces the benchmark exactly:** on all 118 official test images, this
  service's probabilities are bit-identical to the ones the frozen benchmark
  scored (maximum difference 0.0; no mask pixel differs at 0.47). A test
  checks 6 of them wherever the weights and data exist.
- **Two traps, both tested:**
  - ImageNet normalisation happens inside the network only if it is built
    with `normalize_for_imagenet=True`. A network built by hand with defaults
    loads the same weights without error and gives wrong masks. The service
    builds it with surgiseg-ml's `build_model`, and a golden test compares the
    whole path with surgiseg-ml's own `_predict_probabilities`.
  - The run's `config.json` says `threshold: 0.5`; that is the training
    default. The benchmark was scored at 0.47, which is what the service uses.
- **Masks at full size are this service's step.** The benchmark scored masks
  at 128x128. Resizing back to the upload's size is not part of what it
  measured, so full-size masks may score a little differently (on one test
  frame: 0.9833 at 128x128, 0.9776 at 768x576).
- **Not clinical:** see the note at the top.

To run with the real weights, copy them from a local surgiseg-ml checkout
(`make weights`, which reads `SURGISEG_RUN_DIR`). `SURGISERVE_MODEL=fake`
runs without them; with `SURGISERVE_FAKE_MODEL_DELAY_SECONDS`,
`SURGISERVE_JOB_TIMEOUT_SECONDS` and `SURGISERVE_MAX_ATTEMPTS` it is how the
failure paths above were exercised.

## How a job flows

1. `POST /jobs` validates and stores the image, inserts a `queued` row in
   Postgres, then pushes the job id onto a Redis list.
2. A worker claims the id with `BLMOVE`, which atomically moves it from the
   queue to that worker's own processing list.
3. The worker flips the row from `queued` to `running` and counts the
   attempt, with a conditional `UPDATE … WHERE status = 'queued'`. If no row
   changes (the job is already done, or the id was delivered twice) it skips
   the job.
4. It runs the model (loaded once at start-up) under a time limit, writes the
   mask to `masks/<job id>.png`, and records the outcome (below).
5. Only then does it remove the id from its processing list.

The queue carries only ids; Postgres is the record of each job. The queue is
written by hand in `src/surgiserve/job_queue.py` rather than using Celery or
RQ, so every step and every failure path is visible code.

## When things go wrong

```
queued -> running -> succeeded
                  -> failed          input missing or unreadable: retrying can't help
                  -> queued          model error or timeout: retry after a backoff
                  -> dead_lettered   out of attempts (3 by default)
```

| Failure | What happens |
|---------|--------------|
| Model raises, or an attempt runs past the timeout (30 s) | Back to `queued`; the id waits in a Redis sorted set for 2 s, 4 s, … (capped at 60 s, randomly scaled by 0.5-1 so retries do not arrive in step), then returns to the queue |
| The same, on the last attempt | `dead_lettered`, with the last error |
| Stored input missing or unreadable | `failed` at once; no retry |
| Worker killed mid-job | Its heartbeat key (TTL 15 s) expires; another worker resets the job to `queued` and puts the id back on the queue |
| A job that keeps killing its worker | When its worker dies on its last allowed attempt, it is dead-lettered instead of requeued (a crash counts as an attempt) |
| A worker presumed dead wakes up and finishes | Its result is discarded: every final update matches on the attempt number, which a newer attempt has changed |
| Redis down when a job is submitted | Row and file removed, `503`, client retries |
| A job left `queued` with its id in no queue (a process died between a database write and the Redis step) | After 5 minutes the sweep re-enqueues it |
| Database or Redis down while a worker holds a job | The id stays on that worker's processing list; the worker's next maintenance pass, between jobs, puts it back on the queue |
| Worker sent SIGTERM (`docker compose stop`) | Finishes the current job, then exits |

Every worker runs the maintenance pass (promote due retries, recover dead
workers' jobs, sweep stranded ones) every 5 s. It is safe with many workers:
each Redis step is one atomic command or Lua script, and each database change
is a conditional `UPDATE`, so whoever gets there first does the work.

Dead-lettered jobs stay in Postgres (`status = 'dead_lettered'`) rather than
in a second Redis list, so there is one record of them, not two to keep in
step. An operator command lists and requeues them; it is not an HTTP route,
because the API has no authentication:

```bash
docker compose exec worker python -m surgiserve.admin dead-letters
docker compose exec worker python -m surgiserve.admin requeue <job id> …
```

Checked live against the Compose stack, not only in tests: a worker
SIGKILLed mid-job had its job recovered and finished by the other worker; a
killed and immediately restarted worker had its job recovered (see below);
a job whose attempts all timed out retried twice with backoff, was
dead-lettered, then requeued with the command above and succeeded.

**A bug the kill test found.** Worker ids were `hostname-pid`. In a container
the pid is always 1, so a worker that crashed and was restarted by Docker came
back with the dead worker's id. Its fresh heartbeat made the dead worker look
alive, and the job it held stayed `running` indefinitely. Ids now carry a
random suffix per process start.

Known limits:

- The timeout uses `SIGALRM`, which Python handles between bytecodes. The
  model's many short native calls leave room for it, but a single native call
  that never returned would not be interrupted; running the model in a child
  process would cover that.
- A retry starts up to one maintenance interval (5 s) after its backoff ends,
  since that is when due retries are promoted.
- The stranded-job sweep checks each candidate with `LPOS`, which scans the
  queue: fine for a handful of candidates, not for a very long backlog.

## Health and logs

**Two health checks, on purpose.** `/health` is liveness: the process is up.
It touches nothing else, because Docker restarts a container whose liveness
check fails, and restarting the API would not fix a database outage.
`/health/ready` is readiness, for deciding whether to send this instance
traffic. It checks each dependency with a short timeout and answers `503` if
any fails:

| Check | Passes when |
|-------|-------------|
| `database` | Postgres answers a query |
| `schema` | the database is migrated to the revision the code needs (a test keeps that constant equal to the newest migration) |
| `redis` | Redis answers `PING` |
| `storage` | a file can be written to the storage directory and removed |

It also reports, without changing the status code, the live workers and the
model each runs (read from their heartbeats), queue depths and the number of
dead-lettered jobs. No live worker is a warning: the API can still accept
jobs, which wait in the queue.

```json
{"status": "ok", "checks": {"database": {"ok": true, "ms": 1.2}, "schema": {"ok": true, "detail": "at 0003"}, …},
 "workers": {"alive": 1, "models": {"surgiseg-resnet18-unet@5c2308207401": 1}},
 "queue": {"waiting": 0, "delayed": 0, "processing": 0, "dead_lettered": 0}, "warnings": []}
```

Checked live: with Redis stopped, then Postgres stopped, readiness answered
`503` naming the failed dependency (in 0.1 s, not a hang) while liveness
stayed `200`; both returned to `200` when the service came back.

The worker container has its own health check: its main loop touches a file
on every pass, and the check fails if the file is more than 60 s old. That
shows the loop itself is turning, which the Redis heartbeat (sent from a
separate thread) does not.

**Logs** are one JSON object per line, from both services, uvicorn's own
messages included (the one-shot `migrate` step still logs Alembic's plain
text). Every line has `ts`, `level`, `service`, `logger`, `event` and
`message`. Context is attached automatically: every line logged while
handling a request carries its `request_id` (also returned to the client as
`X-Request-ID`, or taken from the request if it sends a plain one), and every
line logged while running a job carries `worker_id`, `job_id` and `attempt`.
So one job can be followed through both services:

```bash
docker compose logs --no-log-prefix api worker | grep <job id>
```

```json
{"service": "api", "event": "job.created", "request_id": "demo-req-1", "job_id": "7ec7d6e1-…", …}
{"service": "worker", "event": "job.started", "worker_id": "08898e3b9c6e-1-baa882cb", "attempt": 1, "job_id": "7ec7d6e1-…", …}
{"service": "worker", "event": "job.succeeded", "attempt": 1, "duration_ms": 308.7, "job_id": "7ec7d6e1-…", …}
```

Requests to `/health*` are logged at DEBUG, since Docker polls them.
`SURGISERVE_LOG_FORMAT=text` gives plain lines for local reading.

## API

| Route | What it does |
|-------|--------------|
| `GET /health` | Liveness: the process is up. Touches nothing else. |
| `GET /health/ready` | Readiness: Postgres, schema revision, Redis and storage all work; `503` if not. Also live workers and their models, queue depths, dead letters. |
| `POST /jobs` | Multipart upload, field `image`. Returns `202` with the job and a `Location` header. |
| `GET /jobs/{id}` | The job's status, what was uploaded, and how it went. |
| `GET /jobs/{id}/mask` | The mask: grayscale PNG the size of the upload, 255 = instrument. `409` until the job has succeeded. |

```bash
curl -F "image=@tests/fixtures/checkerboard-256.png" http://localhost:8000/jobs
```

A moment later, `GET /jobs/{id}`:

```json
{"id": "f9bd6e21-…", "status": "succeeded",
 "created_at": "…", "updated_at": "…", "started_at": "…", "finished_at": "…",
 "model": "surgiseg-resnet18-unet@5c2308207401", "attempts": 1, "error": null,
 "mask_url": "/jobs/f9bd6e21-…/mask",
 "input": {"filename": "checkerboard-256.png", "format": "PNG",
           "width": 256, "height": 256, "size_bytes": 647, "sha256": "e390…"}}
```

`model` names the network and the first 12 characters of its checkpoint's
SHA-256, so every stored mask says which weights produced it. `attempts`
counts starts, including ones cut short by a worker dying.

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

Needs Docker, and the frozen weights in `weights/` (see above).

```bash
make weights                   # copy them from ../surgiseg-ml
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

The image is about 1.4 GB, almost all of it CPU-only PyTorch. The first
build downloads it; later builds reuse the layer.

## Development

Needs Python 3.12.

```bash
make install   # create .venv; CPU-only torch, then the package and dev tools
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
image, waits for the worker, and downloads the mask. CI has no real weights:
it generates a random checkpoint with the frozen architecture inside the
built image (`python -m surgiserve.random_checkpoint`) and passes its hashes
to the worker, so the real inference code runs, on meaningless weights. The
test that compares against the frozen benchmark runs only where the real
checkpoint and dataset exist.

## License

MIT. The model weights are not part of this repository.
