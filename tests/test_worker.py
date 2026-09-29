import io
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from redis import Redis

from surgiserve.job_queue import QUEUE_KEY, JobQueue
from surgiserve.segmentation import FakeModel, SegmentationModel
from surgiserve.settings import Settings
from surgiserve.worker import MAX_ERROR_LENGTH, Outcome, Worker

WORKER_ID = "test-worker"


class BrokenModel:
    name = "broken"

    def __init__(self, message: str = "boom") -> None:
        self.message = message

    def predict(self, image: Image.Image) -> Image.Image:
        raise RuntimeError(self.message)


def half_bright_png() -> bytes:
    image = Image.new("RGB", (64, 48), (20, 20, 20))
    image.paste((240, 240, 240), (32, 0, 64, 48))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_worker(
    app: FastAPI, settings: Settings, model: SegmentationModel | None = None
) -> Worker:
    return Worker(
        queue=app.state.queue,
        session_factory=app.state.session_factory,
        storage_dir=settings.storage_dir,
        model=model or FakeModel(),
        worker_id=WORKER_ID,
        poll_seconds=0.1,
    )


def submit(client: TestClient) -> str:
    response = client.post(
        "/jobs", files={"image": ("frame.png", half_bright_png(), "image/png")}
    )
    assert response.status_code == 202
    job_id: str = response.json()["id"]
    return job_id


@pytest.fixture
def queue(app: FastAPI) -> JobQueue:
    job_queue: JobQueue = app.state.queue
    return job_queue


def test_submitting_a_job_puts_its_id_on_the_queue(
    client: TestClient, queue: JobQueue
) -> None:
    job_id = submit(client)

    assert queue.waiting() == [job_id]


def test_worker_runs_a_job_end_to_end(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)

    assert make_worker(app, settings).run_once() == Outcome.SUCCEEDED

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    assert job["model"] == "fake-brightness-threshold"
    assert job["error"] is None
    assert job["created_at"] <= job["started_at"] <= job["finished_at"]
    with Image.open(settings.storage_dir / "masks" / f"{job_id}.png") as mask:
        assert (mask.mode, mask.size) == ("L", (64, 48))
        assert mask.getpixel((10, 10)) == 0
        assert mask.getpixel((50, 10)) == 255
    assert queue.waiting() == []
    assert queue.processing(WORKER_ID) == []


def test_run_once_returns_none_when_the_queue_is_empty(
    app: FastAPI, settings: Settings
) -> None:
    assert make_worker(app, settings).run_once() is None


def test_a_job_delivered_twice_runs_once(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    queue.enqueue(uuid.UUID(job_id))  # the same id again
    worker = make_worker(app, settings)

    assert worker.run_once() == Outcome.SUCCEEDED
    assert worker.run_once() == Outcome.SKIPPED
    assert queue.processing(WORKER_ID) == []


def test_an_id_with_no_job_is_skipped_and_acked(
    app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    queue.enqueue(uuid.uuid4())

    assert make_worker(app, settings).run_once() == Outcome.SKIPPED
    assert queue.processing(WORKER_ID) == []


def test_a_malformed_queue_entry_is_dropped(
    app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    redis: Redis = app.state.redis
    redis.lpush(QUEUE_KEY, "not-a-uuid")

    assert make_worker(app, settings).run_once() == Outcome.SKIPPED
    assert queue.processing(WORKER_ID) == []


def test_a_model_error_fails_the_job(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)

    assert make_worker(app, settings, BrokenModel()).run_once() == Outcome.FAILED

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error"] == "RuntimeError: boom"
    assert job["finished_at"] is not None
    assert not (settings.storage_dir / "masks" / f"{job_id}.png").exists()
    assert queue.processing(WORKER_ID) == []


def test_stored_errors_are_truncated(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    job_id = submit(client)

    make_worker(app, settings, BrokenModel("x" * 5000)).run_once()

    assert len(client.get(f"/jobs/{job_id}").json()["error"]) == MAX_ERROR_LENGTH


def test_a_missing_input_file_fails_the_job(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    job_id = submit(client)
    (settings.storage_dir / "inputs" / f"{job_id}.png").unlink()

    assert make_worker(app, settings).run_once() == Outcome.FAILED
    assert client.get(f"/jobs/{job_id}").json()["error"].startswith("FileNotFoundError")


def test_an_infrastructure_error_keeps_the_id_on_the_processing_list(
    client: TestClient,
    app: FastAPI,
    settings: Settings,
    queue: JobQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = submit(client)
    worker = make_worker(app, settings)

    def database_down(_job_id: uuid.UUID) -> str | None:
        raise ConnectionError("database went away")

    monkeypatch.setattr(worker, "_start", database_down)

    with pytest.raises(ConnectionError):
        worker.run_once()

    # Not lost and not failed: still held, for recovery in Milestone 4.
    assert queue.processing(WORKER_ID) == [job_id]
    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"


def test_run_processes_jobs_until_told_to_stop(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    job_ids = [submit(client) for _ in range(3)]
    stop = threading.Event()
    thread = threading.Thread(target=make_worker(app, settings).run, args=(stop,))
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            statuses = {client.get(f"/jobs/{i}").json()["status"] for i in job_ids}
            if statuses == {"succeeded"}:
                break
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert statuses == {"succeeded"}
    assert not thread.is_alive()
