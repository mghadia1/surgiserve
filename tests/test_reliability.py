"""Retries, timeouts, dead letters, crash recovery and stranded jobs."""

import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text
from support import make_worker, submit

from surgiserve import admin
from surgiserve.job_queue import JobQueue, heartbeat_key
from surgiserve.segmentation import FakeModel
from surgiserve.settings import Settings
from surgiserve.worker import (
    JobTimeout,
    Outcome,
    Policy,
    backoff_seconds,
    new_worker_id,
    time_limit,
)

# No waiting between attempts, so the tests run fast.
QUICK = Policy(retry_base_seconds=0.0, retry_max_seconds=0.0)


class FlakyModel:
    """Fails a set number of times, then works."""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures

    def predict(self, image: Image.Image) -> Image.Image:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("transient")
        return FakeModel().predict(image)


class SlowModel:
    name = "slow"

    def predict(self, image: Image.Image) -> Image.Image:
        time.sleep(5)
        return FakeModel().predict(image)


@pytest.fixture
def queue(app: FastAPI) -> JobQueue:
    job_queue: JobQueue = app.state.queue
    return job_queue


def job(client: TestClient, job_id: str) -> dict[str, object]:
    body: dict[str, object] = client.get(f"/jobs/{job_id}").json()
    return body


def redis_now(queue: JobQueue) -> float:
    seconds, microseconds = queue.redis.time()
    return seconds + microseconds / 1e6


def set_column(app: FastAPI, job_id: str, assignment: str) -> None:
    with app.state.engine.begin() as connection:
        connection.execute(
            text(f"UPDATE jobs SET {assignment} WHERE id = :id"), {"id": job_id}
        )


# --- backoff and timeouts -----------------------------------------------------


def test_backoff_doubles_is_capped_and_jittered() -> None:
    policy = Policy(retry_base_seconds=2.0, retry_max_seconds=10.0)

    assert backoff_seconds(1, policy, rand=lambda: 0.0) == 1.0  # 2s * 0.5
    assert backoff_seconds(2, policy, rand=lambda: 0.0) == 2.0  # 4s * 0.5
    assert backoff_seconds(3, policy, rand=lambda: 0.999) == pytest.approx(8.0, 0.01)
    assert backoff_seconds(9, policy, rand=lambda: 0.999) == pytest.approx(10.0, 0.01)


def test_time_limit_interrupts_a_slow_block() -> None:
    started = time.monotonic()

    with pytest.raises(JobTimeout), time_limit(0.2):
        time.sleep(5)

    assert time.monotonic() - started < 1


def test_time_limit_refuses_to_run_off_the_main_thread() -> None:
    errors: list[BaseException] = []

    def attempt() -> None:
        try:
            with time_limit(1):
                pass
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join()

    assert isinstance(errors[0], RuntimeError)


def test_a_worker_with_timeouts_will_not_run_off_the_main_thread(
    app: FastAPI, settings: Settings
) -> None:
    worker = make_worker(app, settings)  # default policy: 30s timeout
    errors: list[BaseException] = []

    def run() -> None:
        try:
            worker.run(threading.Event())
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=5)

    assert isinstance(errors[0], RuntimeError)


# --- retries and dead letters -------------------------------------------------


def test_a_failed_attempt_is_scheduled_for_retry(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    worker = make_worker(app, settings, FlakyModel(failures=1))

    before = redis_now(queue)
    assert worker.run_once() == Outcome.RETRY

    state = job(client, job_id)
    assert (state["status"], state["attempts"]) == ("queued", 1)
    assert state["error"] == "RuntimeError: transient"
    assert queue.waiting() == []
    assert queue.processing("test-worker") == []
    # Attempt 1 waits 2s * [0.5, 1): due 1-2 seconds from now, on Redis's
    # clock (the one due times use), not this machine's.
    due = queue.delayed()[job_id]
    assert before + 1.0 <= due <= redis_now(queue) + 2.0


def test_a_retry_runs_once_it_is_due(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    worker = make_worker(app, settings, FlakyModel(failures=1), policy=QUICK)
    assert worker.run_once() == Outcome.RETRY

    assert worker.maintain().promoted == 1
    assert worker.run_once() == Outcome.SUCCEEDED

    state = job(client, job_id)
    assert (state["status"], state["attempts"]) == ("succeeded", 2)
    assert state["error"] is None
    assert queue.delayed() == {}


def test_out_of_attempts_the_job_is_dead_lettered(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    worker = make_worker(app, settings, FlakyModel(failures=99), policy=QUICK)

    outcomes = []
    for _ in range(3):
        outcomes.append(worker.run_once())
        worker.maintain()

    assert outcomes == [Outcome.RETRY, Outcome.RETRY, Outcome.DEAD_LETTERED]
    state = job(client, job_id)
    assert (state["status"], state["attempts"]) == ("dead_lettered", 3)
    assert state["error"] == "RuntimeError: transient"
    assert queue.waiting() == [] and queue.delayed() == {}
    assert client.get(f"/jobs/{job_id}/mask").status_code == 409


def test_a_timed_out_attempt_is_retried(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    job_id = submit(client)
    worker = make_worker(
        app, settings, SlowModel(), policy=Policy(job_timeout_seconds=0.2)
    )

    started = time.monotonic()
    assert worker.run_once() == Outcome.RETRY

    assert time.monotonic() - started < 2
    assert job(client, job_id)["error"] == "JobTimeout: attempt exceeded 0.2s"


def test_an_unreadable_input_fails_without_retrying(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    (settings.storage_dir / "inputs" / f"{job_id}.png").write_bytes(b"damaged")

    assert make_worker(app, settings).run_once() == Outcome.FAILED

    state = job(client, job_id)
    assert (state["status"], state["attempts"]) == ("failed", 1)
    assert str(state["error"]).startswith("UnidentifiedImageError")
    assert queue.delayed() == {}


def test_a_stale_worker_cannot_overwrite_a_newer_attempt(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    job_id = submit(client)
    stale = make_worker(app, settings, worker_id="stale")
    fresh = make_worker(app, settings, worker_id="fresh")

    _, first_attempt = stale._start(uuid.UUID(job_id)) or ("", 0)
    # The stale worker is presumed dead; its job is requeued and restarted.
    set_column(app, job_id, "status = 'queued'")
    _, second_attempt = fresh._start(uuid.UUID(job_id)) or ("", 0)
    assert (first_attempt, second_attempt) == (1, 2)

    from surgiserve.models import JobStatus

    written = stale._finish(uuid.UUID(job_id), first_attempt, JobStatus.FAILED)

    assert written is False
    assert job(client, job_id)["status"] == "running"  # the fresh attempt's


# --- crash recovery -----------------------------------------------------------


def hold_as_dead_worker(
    app: FastAPI, settings: Settings, queue: JobQueue, *, start: bool = True
) -> str:
    """Claim (and optionally start) a job as a worker that then 'dies':
    no heartbeat key, id left on its processing list."""

    dead = make_worker(app, settings, worker_id="dead-worker")
    raw = queue.claim("dead-worker", 0.1)
    assert raw is not None
    if start:
        dead._start(uuid.UUID(raw))
    return raw


def test_a_dead_workers_job_is_requeued_and_then_completed(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    hold_as_dead_worker(app, settings, queue)
    assert job(client, job_id)["status"] == "running"
    survivor = make_worker(app, settings, worker_id="survivor")

    assert survivor.maintain().recovered == 1

    state = job(client, job_id)
    assert state["status"] == "queued"
    assert state["error"] == "worker dead-worker stopped responding"
    assert queue.processing("dead-worker") == []
    assert queue.waiting() == [job_id]

    assert survivor.run_once() == Outcome.SUCCEEDED
    assert job(client, job_id)["attempts"] == 2


def test_a_job_claimed_but_not_started_is_recovered(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    hold_as_dead_worker(app, settings, queue, start=False)

    assert make_worker(app, settings, worker_id="survivor").maintain().recovered == 1
    assert queue.waiting() == [job_id]


def test_a_live_workers_jobs_are_left_alone(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    queue.beat("busy-worker", ttl_seconds=30)
    busy = make_worker(app, settings, worker_id="busy-worker")
    raw = queue.claim("busy-worker", 0.1)
    assert raw is not None
    busy._start(uuid.UUID(raw))

    assert make_worker(app, settings, worker_id="other").maintain().recovered == 0
    assert queue.processing("busy-worker") == [job_id]
    assert job(client, job_id)["status"] == "running"


def test_a_job_that_keeps_killing_workers_is_dead_lettered(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    hold_as_dead_worker(app, settings, queue)
    set_column(app, job_id, "attempts = 3")  # its last allowed attempt

    result = make_worker(app, settings, worker_id="survivor").maintain()

    assert (result.recovered, result.dead_lettered) == (0, 1)
    state = job(client, job_id)
    assert state["status"] == "dead_lettered"
    assert "stopped responding during the last allowed attempt" in str(state["error"])
    assert queue.processing("dead-worker") == [] and queue.waiting() == []


def test_run_keeps_a_heartbeat_while_it_runs(
    app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    worker = make_worker(
        app,
        settings,
        policy=Policy(job_timeout_seconds=None, heartbeat_seconds=0.05),
    )
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,))
    thread.start()
    try:
        time.sleep(0.3)
        assert queue.is_alive("test-worker")
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not queue.is_alive("test-worker")


def test_each_process_start_gets_a_new_worker_id() -> None:
    # In a container the pid is always 1; a restarted worker must still not
    # reuse the id of the one that died.
    first, second = new_worker_id(), new_worker_id()

    assert first != second
    assert first.rsplit("-", 1)[0] == second.rsplit("-", 1)[0]  # host and pid


def test_heartbeats_expire(app: FastAPI, queue: JobQueue) -> None:
    queue.beat("short-lived", ttl_seconds=0.1)
    assert queue.is_alive("short-lived")

    time.sleep(0.25)

    assert not queue.is_alive("short-lived")
    redis = app.state.redis
    assert not redis.exists(heartbeat_key("short-lived"))


# --- stranded jobs ------------------------------------------------------------


def test_a_stranded_job_is_re_enqueued(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    queue.redis.delete("surgiserve:queue")  # its id never made it to Redis
    set_column(app, job_id, "updated_at = now() - interval '10 minutes'")
    worker = make_worker(app, settings, worker_id="sweeper")

    assert worker.maintain().requeued == 1
    assert queue.waiting() == [job_id]
    # Touched, and now waiting: the next sweep does not add a duplicate.
    assert worker.maintain().requeued == 0
    assert queue.waiting() == [job_id]


def test_a_recent_or_already_waiting_job_is_not_touched(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    waiting = submit(client)
    set_column(app, waiting, "updated_at = now() - interval '10 minutes'")
    recent = submit(client)
    queue.redis.lrem("surgiserve:queue", 1, recent)

    assert make_worker(app, settings, worker_id="sweeper").maintain().requeued == 0
    assert queue.waiting() == [waiting]


# --- operator commands --------------------------------------------------------


def test_admin_lists_and_requeues_dead_letters(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    worker = make_worker(app, settings, FlakyModel(failures=3), policy=QUICK)
    for _ in range(3):
        worker.run_once()
        worker.maintain()
    assert job(client, job_id)["status"] == "dead_lettered"

    listed = admin.dead_letters(app.state.session_factory)
    assert [str(entry.id) for entry in listed] == [job_id]

    requeued = admin.requeue(
        app.state.session_factory, queue, [uuid.UUID(job_id), uuid.uuid4()]
    )

    assert [str(entry) for entry in requeued] == [job_id]
    state = job(client, job_id)
    assert (state["status"], state["attempts"], state["error"]) == ("queued", 0, None)
    assert queue.waiting() == [job_id]
    # The model has recovered by now: the requeued job succeeds.
    assert worker.run_once() == Outcome.SUCCEEDED


def test_admin_does_not_requeue_a_succeeded_job(
    client: TestClient, app: FastAPI, settings: Settings, queue: JobQueue
) -> None:
    job_id = submit(client)
    make_worker(app, settings).run_once()

    assert admin.requeue(app.state.session_factory, queue, [uuid.UUID(job_id)]) == []
    assert job(client, job_id)["status"] == "succeeded"


def test_the_database_accepts_dead_lettered(app: FastAPI) -> None:
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (id, status, input_path, image_format, width, "
                "height, size_bytes, sha256) "
                "VALUES (:id, 'dead_lettered', 'x', 'PNG', 1, 1, 1, 'x')"
            ),
            {"id": uuid.uuid4()},
        )
