"""The worker: take job ids off the queue, run the model, record the outcome.

Run with ``python -m surgiserve.worker``. Several can run at once; each claims
its own jobs, and each also runs a periodic maintenance pass (promote due
retries, recover dead workers' jobs, requeue stranded ones). That pass is safe
to run from every worker at once: each Redis step is atomic and each database
change is a conditional UPDATE, so whichever worker gets there first does the
work and the rest find nothing to do.
"""

import dataclasses
import io
import logging
import os
import random
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from types import FrameType, TracebackType
from uuid import UUID, uuid4

from PIL import Image
from redis import Redis
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .job_queue import JobQueue
from .logs import configure_logging, log_context
from .models import Job, JobStatus
from .segmentation import FakeModel, SegmentationModel
from .settings import Settings
from .storage import save_atomically

logger = logging.getLogger("surgiserve.worker")

# Errors are stored for people to read; a whole traceback does not belong in
# the jobs table.
MAX_ERROR_LENGTH = 1000
# Pause after an infrastructure error (database or Redis down) so the loop
# does not spin.
ERROR_BACKOFF_SECONDS = 1.0


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    # A problem retrying cannot fix, such as a missing or unreadable input.
    FAILED = "failed"
    # Failed this attempt; scheduled to run again after a backoff.
    RETRY = "retry"
    # Failed its last allowed attempt.
    DEAD_LETTERED = "dead_lettered"
    # Not queued any more (already done, or delivered twice) or never existed.
    SKIPPED = "skipped"


class PermanentJobError(Exception):
    """Retrying cannot help: the job's own input is missing or unreadable."""


class JobTimeout(Exception):
    """An attempt ran past the job timeout."""


@dataclass(frozen=True)
class Policy:
    """Retry, timeout and liveness settings; see ``Settings`` for meanings."""

    max_attempts: int = 3
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 60.0
    job_timeout_seconds: float | None = 30.0
    heartbeat_seconds: float = 5.0
    heartbeat_ttl_seconds: float = 15.0
    maintenance_seconds: float = 5.0
    stranded_after_seconds: float = 300.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "Policy":
        return cls(
            max_attempts=settings.max_attempts,
            retry_base_seconds=settings.retry_base_seconds,
            retry_max_seconds=settings.retry_max_seconds,
            job_timeout_seconds=settings.job_timeout_seconds or None,
            heartbeat_seconds=settings.heartbeat_seconds,
            heartbeat_ttl_seconds=settings.heartbeat_ttl_seconds,
            maintenance_seconds=settings.maintenance_seconds,
            stranded_after_seconds=settings.stranded_after_seconds,
        )


def backoff_seconds(
    attempt: int, policy: Policy, rand: Callable[[], float] = random.random
) -> float:
    """Delay before the retry that follows ``attempt`` (1-based).

    Exponential, capped, then scaled by a random factor in [0.5, 1): jobs that
    failed together (say, during one outage) do not all retry in the same
    instant and cause the next one.
    """

    ceiling = min(
        policy.retry_max_seconds, policy.retry_base_seconds * 2.0 ** (attempt - 1)
    )
    return ceiling * (0.5 + rand() / 2)


@contextmanager
def time_limit(seconds: float | None) -> Iterator[None]:
    """Raise JobTimeout in this thread if the block runs longer than
    ``seconds``.

    It uses SIGALRM, so it works only in the main thread, which is where the
    worker runs jobs. Python runs the signal handler between bytecodes: a
    model spends its time in many short native calls, so the timeout lands
    promptly, but a single native call that never returns would not be
    interrupted. Isolating the model in a child process would cover that.
    """

    if not seconds:
        yield
        return
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("job timeouts need the main thread")

    def on_alarm(_signum: int, _frame: FrameType | None) -> None:
        raise JobTimeout(f"attempt exceeded {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:MAX_ERROR_LENGTH]


class Heartbeat:
    """Keeps the worker's liveness key fresh from a background thread, so a
    long job does not make a healthy worker look dead. The key also carries
    what the readiness check reports: the model this worker runs."""

    def __init__(
        self, queue: JobQueue, worker_id: str, policy: Policy, info: dict[str, str]
    ) -> None:
        self.queue = queue
        self.worker_id = worker_id
        self.policy = policy
        self.info = info
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "Heartbeat":
        # Beat before the first claim, so the worker never holds a job while
        # looking dead.
        self.queue.beat(self.worker_id, self.policy.heartbeat_ttl_seconds, self.info)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.policy.heartbeat_seconds):
            try:
                self.queue.beat(
                    self.worker_id, self.policy.heartbeat_ttl_seconds, self.info
                )
            except Exception:
                logger.exception(
                    "heartbeat failed", extra={"event": "worker.heartbeat_failed"}
                )

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()
        self.queue.forget(self.worker_id)


@dataclass(frozen=True)
class Maintenance:
    promoted: int = 0
    recovered: int = 0
    dead_lettered: int = 0
    requeued: int = 0


class Worker:
    def __init__(
        self,
        *,
        queue: JobQueue,
        session_factory: sessionmaker[Session],
        storage_dir: Path,
        model: SegmentationModel,
        worker_id: str,
        poll_seconds: float = 1.0,
        policy: Policy | None = None,
        liveness_file: Path | None = None,
    ) -> None:
        self.queue = queue
        self.session_factory = session_factory
        self.storage_dir = storage_dir
        self.model = model
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds
        self.policy = policy or Policy()
        self.liveness_file = liveness_file

    # --- the loop -----------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Work until ``stop`` is set. A job in progress is always finished;
        the event is checked only between jobs."""

        if (
            self.policy.job_timeout_seconds
            and threading.current_thread() is not threading.main_thread()
        ):
            # Checked here, before any job is claimed. Raised inside a job, it
            # would look like a job failure and be retried until dead-lettered.
            raise RuntimeError("job timeouts use SIGALRM and need the main thread")
        with (
            log_context(worker_id=self.worker_id),
            Heartbeat(
                self.queue, self.worker_id, self.policy, {"model": self.model.name}
            ),
        ):
            logger.info(
                "worker started",
                extra={"event": "worker.started", "model": self.model.name},
            )
            next_maintenance = 0.0
            while not stop.is_set():
                self._touch_liveness_file()
                try:
                    if time.monotonic() >= next_maintenance:
                        self.maintain()
                        next_maintenance = (
                            time.monotonic() + self.policy.maintenance_seconds
                        )
                    self.run_once()
                except Exception:
                    # Database or Redis trouble, not a bad job: a claimed id
                    # stays on this worker's processing list (see run_once).
                    logger.exception(
                        "infrastructure error", extra={"event": "worker.error"}
                    )
                    stop.wait(ERROR_BACKOFF_SECONDS)
            logger.info("worker stopped", extra={"event": "worker.stopped"})

    def _touch_liveness_file(self) -> None:
        if self.liveness_file is not None:
            self.liveness_file.touch()

    def run_once(self) -> Outcome | None:
        """Claim and handle at most one job. Returns None if none arrived."""

        raw_job_id = self.queue.claim(self.worker_id, self.poll_seconds)
        if raw_job_id is None:
            return None
        try:
            job_id = UUID(raw_job_id)
        except ValueError:
            logger.error(
                "dropping malformed queue entry %r",
                raw_job_id,
                extra={"event": "queue.malformed_entry"},
            )
            self.queue.ack(self.worker_id, raw_job_id)
            return Outcome.SKIPPED

        # If process() raises, the id is deliberately not acknowledged: it
        # stays on the processing list, where recovery can find it.
        outcome = self.process(job_id)
        self.queue.ack(self.worker_id, raw_job_id)
        return outcome

    # --- one job ------------------------------------------------------------

    def process(self, job_id: UUID) -> Outcome:
        with log_context(job_id=str(job_id)):
            started_job = self._start(job_id)
            if started_job is None:
                logger.info("job skipped: not queued", extra={"event": "job.skipped"})
                return Outcome.SKIPPED
            input_path, attempt = started_job
            with log_context(attempt=attempt):
                logger.info("job started", extra={"event": "job.started"})
                return self._attempt(job_id, input_path, attempt)

    def _attempt(self, job_id: UUID, input_path: str, attempt: int) -> Outcome:
        started = time.perf_counter()
        try:
            with time_limit(self.policy.job_timeout_seconds):
                result_path = self._segment(job_id, input_path)
        except PermanentJobError as error:
            self._finish(job_id, attempt, JobStatus.FAILED, error=str(error))
            logger.warning(
                "job failed permanently: %s",
                error,
                extra={"event": "job.failed", "error": str(error)},
            )
            return Outcome.FAILED
        except Exception as error:
            # The model raised, or the attempt timed out. Possibly transient.
            return self._retry_or_dead_letter(job_id, attempt, describe(error))

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        self._finish(job_id, attempt, JobStatus.SUCCEEDED, result_path=result_path)
        logger.info(
            "job succeeded in %.0f ms",
            duration_ms,
            extra={"event": "job.succeeded", "duration_ms": duration_ms},
        )
        return Outcome.SUCCEEDED

    def _start(self, job_id: UUID) -> tuple[str, int] | None:
        """Move the job from queued to running and count the attempt.

        Returns its input path and this attempt's number, or None if it was not
        queued, so a second delivery of the same id does nothing."""

        with self.session_factory() as session:
            row = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                .values(
                    status=JobStatus.RUNNING,
                    attempts=Job.attempts + 1,
                    started_at=func.now(),
                    model_name=self.model.name,
                )
                .returning(Job.input_path, Job.attempts)
            ).one_or_none()
            session.commit()
        return None if row is None else (row.input_path, row.attempts)

    def _segment(self, job_id: UUID, input_path: str) -> str:
        try:
            with Image.open(self.storage_dir / input_path) as image:
                # No EXIF rotation: surgiseg-ml does not apply it either, and
                # the mask must line up with the pixels as stored.
                rgb = image.convert("RGB")
        except (OSError, SyntaxError, ValueError) as error:
            # Missing or unreadable input. The upload was validated, so this
            # means the stored file was lost or damaged; retrying won't help.
            raise PermanentJobError(describe(error)) from error
        mask = self.model.predict(rgb)
        buffer = io.BytesIO()
        mask.save(buffer, format="PNG")
        result_path = f"masks/{job_id}.png"
        save_atomically(self.storage_dir, result_path, buffer.getvalue())
        return result_path

    def _retry_or_dead_letter(self, job_id: UUID, attempt: int, error: str) -> Outcome:
        if attempt >= self.policy.max_attempts:
            self._finish(job_id, attempt, JobStatus.DEAD_LETTERED, error=error)
            logger.error(
                "job dead-lettered after %d attempts: %s",
                attempt,
                error,
                extra={"event": "job.dead_lettered", "error": error},
            )
            return Outcome.DEAD_LETTERED

        delay = backoff_seconds(attempt, self.policy)
        with self.session_factory() as session:
            requeued = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING,
                    Job.attempts == attempt,
                )
                .values(status=JobStatus.QUEUED, error=error)
                .returning(Job.id)
            ).one_or_none()
            session.commit()
        # Database first, then Redis: if the process dies in between, the id
        # is still on this worker's processing list and recovery requeues it.
        if requeued is not None:
            self.queue.schedule(job_id, delay)
        logger.warning(
            "attempt failed, retrying in %.1fs: %s",
            delay,
            error,
            extra={
                "event": "job.retry_scheduled",
                "retry_in_s": round(delay, 2),
                "error": error,
            },
        )
        return Outcome.RETRY

    def _finish(
        self,
        job_id: UUID,
        attempt: int,
        status: JobStatus,
        *,
        result_path: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Record the outcome of ``attempt``. Returns False if the job has
        moved on since (a stale worker): its result is then discarded.

        The attempt number works as a fencing token. A worker that stalled
        long enough to be presumed dead may wake up and finish after its job
        was requeued and restarted elsewhere; matching on the attempt keeps
        that late result from overwriting the newer one."""

        with self.session_factory() as session:
            finished = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING,
                    Job.attempts == attempt,
                )
                .values(
                    status=status,
                    result_path=result_path,
                    error=error,
                    finished_at=func.now(),
                )
                .returning(Job.id)
            ).one_or_none()
            session.commit()
        if finished is None:
            logger.warning(
                "attempt finished late; a newer attempt owns the job",
                extra={"event": "job.stale_result_discarded"},
            )
        return finished is not None

    # --- maintenance --------------------------------------------------------

    def maintain(self) -> Maintenance:
        promoted = self.queue.promote_due()
        recovered, dead_lettered = self.recover_dead_workers()
        requeued = self.requeue_stranded()
        result = Maintenance(promoted, recovered, dead_lettered, requeued)
        if result != Maintenance():
            logger.info(
                "maintenance moved jobs",
                extra={"event": "maintenance", **dataclasses.asdict(result)},
            )
        return result

    def recover_dead_workers(self) -> tuple[int, int]:
        """Requeue the jobs held by workers whose heartbeat has expired, and
        any left on this worker's own list.

        This worker's own list counts too. Maintenance runs between jobs, in
        the thread that runs them, so at this moment its list should be empty;
        an id still there was left by an attempt that hit a database or Redis
        error. Without this, that id would wait for this worker to die.

        Returns (requeued, dead-lettered)."""

        recovered = dead_lettered = 0
        for worker_id in list(self.queue.workers_holding_jobs()):
            if worker_id != self.worker_id and self.queue.is_alive(worker_id):
                continue
            for raw_job_id in self.queue.processing(worker_id):
                try:
                    job_id = UUID(raw_job_id)
                except ValueError:
                    self.queue.drop_from_processing(worker_id, raw_job_id)
                    continue
                if self._dead_letter_if_out_of_attempts(job_id, worker_id):
                    # A job that keeps killing its worker must not be handed
                    # to the next one forever.
                    self.queue.drop_from_processing(worker_id, raw_job_id)
                    dead_lettered += 1
                    continue
                # Database first: the row must say queued before the id is
                # back where a worker can claim it, or that worker would skip
                # it and the job would be stuck in running.
                self._reset_to_queued(job_id, worker_id)
                if self.queue.return_to_queue(worker_id, raw_job_id):
                    recovered += 1
                    logger.warning(
                        "requeued a job held by a dead worker",
                        extra={
                            "event": "job.recovered",
                            "job_id": str(job_id),
                            "dead_worker_id": worker_id,
                        },
                    )
        return recovered, dead_lettered

    def _dead_letter_if_out_of_attempts(self, job_id: UUID, worker_id: str) -> bool:
        with self.session_factory() as session:
            row = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING,
                    Job.attempts >= self.policy.max_attempts,
                )
                .values(
                    status=JobStatus.DEAD_LETTERED,
                    error=(
                        f"worker {worker_id} stopped responding during the last "
                        "allowed attempt"
                    ),
                    finished_at=func.now(),
                )
                .returning(Job.id)
            ).one_or_none()
            session.commit()
        return row is not None

    def _reset_to_queued(self, job_id: UUID, worker_id: str) -> None:
        # Only a running job is reset. If the dead worker had claimed the id
        # but not started it, the row is still queued; if it finished but died
        # before acknowledging, the row is final. Either way the id goes back
        # on the queue and the next worker skips or runs it as appropriate.
        with self.session_factory() as session:
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
                .values(
                    status=JobStatus.QUEUED,
                    error=f"worker {worker_id} stopped responding",
                )
            )
            session.commit()

    def requeue_stranded(self, limit: int = 100) -> int:
        """Re-enqueue jobs left queued with their id in no queue.

        That happens if the API committed a job but could not enqueue it and
        also could not undo it, or if a process died between a database
        update and the matching Redis step. A duplicate id is harmless (the
        conditional start skips it), so this errs towards re-enqueueing."""

        cutoff = func.now() - timedelta(seconds=self.policy.stranded_after_seconds)
        with self.session_factory() as session:
            candidates = session.scalars(
                select(Job.id)
                .where(Job.status == JobStatus.QUEUED, Job.updated_at < cutoff)
                .order_by(Job.updated_at)
                .limit(limit)
            ).all()
            stranded = [
                job_id for job_id in candidates if not self.queue.is_waiting(job_id)
            ]
            if stranded:
                # Touch them, so the next sweep leaves them alone for a while.
                session.execute(
                    update(Job)
                    .where(Job.id.in_(stranded))
                    .values(updated_at=func.now())
                )
                session.commit()
        for job_id in stranded:
            self.queue.enqueue(job_id)
            logger.warning(
                "re-enqueued a stranded job",
                extra={"event": "job.unstranded", "job_id": str(job_id)},
            )
        return len(stranded)


def new_worker_id() -> str:
    """An id unique to this process start.

    Host name and pid alone are not enough: in a container the pid is always
    1, so a worker that crashes and is restarted by Docker would come back
    with the dead one's id. Its fresh heartbeat would then make the dead
    worker look alive, and the job it held would never be recovered. (Seen
    for real: a job stayed running indefinitely after a kill and restart.)
    """

    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


def limit_threads(threads: int) -> None:
    """Limit PyTorch to ``threads`` compute threads (0: leave the default).

    ``torch.set_num_threads`` alone is not enough in this image: on ARM the
    convolutions run through oneDNN, whose thread pool is sized from
    ``OMP_NUM_THREADS`` when it first starts. Measured with the setting at 1
    and no load: 68% of the forward pass's CPU time was on other threads, a
    worker "limited to 1 thread" used about 2.5 CPUs. So the variable is set
    too, and it only works if torch has not been imported yet.
    """

    if not threads:
        return
    if "torch" in sys.modules:
        logger.warning(
            "torch was imported before the thread limit was set; "
            "OMP_NUM_THREADS will not take effect",
            extra={"event": "worker.thread_limit_late"},
        )
    os.environ["OMP_NUM_THREADS"] = str(threads)
    import torch

    torch.set_num_threads(threads)


def build_model(settings: Settings) -> SegmentationModel:
    if settings.model == "fake":
        return FakeModel(delay_seconds=settings.fake_model_delay_seconds)
    limit_threads(settings.torch_threads)
    # Imported here, after the thread limit, and only in the worker: the API
    # process never runs the model and never loads torch.
    from .surgiseg_model import SurgisegModel

    # Raises if either file's hash is wrong: better not to start than to
    # serve masks from weights nobody evaluated.
    return SurgisegModel.load(
        settings.model_checkpoint_path,
        settings.model_config_path,
        checkpoint_sha256=settings.model_checkpoint_sha256,
        config_sha256=settings.model_config_sha256,
        threshold=settings.model_threshold,
        flip_tta=settings.model_flip_tta,
    )


def main() -> None:
    settings = Settings()
    configure_logging(
        "worker", log_format=settings.log_format, level=settings.log_level
    )
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    worker = Worker(
        queue=JobQueue(Redis.from_url(settings.redis_url, decode_responses=True)),
        session_factory=sessionmaker(engine, expire_on_commit=False),
        storage_dir=settings.storage_dir,
        model=build_model(settings),
        worker_id=new_worker_id(),
        poll_seconds=settings.worker_poll_seconds,
        policy=Policy.from_settings(settings),
        liveness_file=settings.worker_liveness_file,
    )

    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info(
            "received %s; finishing the current job, then stopping",
            signal.Signals(signum).name,
            extra={"event": "worker.stopping"},
        )
        stop.set()

    # `docker compose stop` sends SIGTERM; Ctrl-C sends SIGINT.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    worker.run(stop)
    engine.dispose()


if __name__ == "__main__":
    main()
