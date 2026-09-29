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
| 4 | Retries with backoff, timeouts, dead-letter queue, worker crash recovery | Not started |
| 5 | Structured JSON logs and a deep `/health` check | Not started |
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
runs without them.

## How a job flows

1. `POST /jobs` validates and stores the image, inserts a `queued` row in
   Postgres, then pushes the job id onto a Redis list.
2. A worker claims the id with `BLMOVE`, which atomically moves it from the
   queue to that worker's own processing list.
3. The worker flips the row from `queued` to `running` with a conditional
   `UPDATE … WHERE status = 'queued'`. If no row changes (the job is already
   done, or the id was delivered twice) it skips the job.
4. It runs the model (loaded once at start-up), writes the mask to
   `masks/<job id>.png`, and marks the
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
| `GET /jobs/{id}/mask` | The mask: grayscale PNG the size of the upload, 255 = instrument. `409` until the job has succeeded. |

```bash
curl -F "image=@tests/fixtures/checkerboard-256.png" http://localhost:8000/jobs
```

A moment later, `GET /jobs/{id}`:

```json
{"id": "f9bd6e21-…", "status": "succeeded",
 "created_at": "…", "updated_at": "…", "started_at": "…", "finished_at": "…",
 "model": "surgiseg-resnet18-unet@5c2308207401", "error": null,
 "mask_url": "/jobs/f9bd6e21-…/mask",
 "input": {"filename": "checkerboard-256.png", "format": "PNG",
           "width": 256, "height": 256, "size_bytes": 647, "sha256": "e390…"}}
```

`model` names the network and the first 12 characters of its checkpoint's
SHA-256, so every stored mask says which weights produced it.

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
