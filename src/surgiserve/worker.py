"""The worker: take job ids off the queue, run the model, record the outcome.

Run with ``python -m surgiserve.worker``. Several can run at once; each claims
its own jobs.
"""

import io
import logging
import os
import signal
import socket
import threading
import time
from enum import StrEnum
from pathlib import Path
from types import FrameType
from uuid import UUID

from PIL import Image
from redis import Redis
from sqlalchemy import create_engine, func, update
from sqlalchemy.orm import Session, sessionmaker

from .job_queue import JobQueue
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
    FAILED = "failed"
    # Not queued any more (already done, or delivered twice) or never existed.
    SKIPPED = "skipped"


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
    ) -> None:
        self.queue = queue
        self.session_factory = session_factory
        self.storage_dir = storage_dir
        self.model = model
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds

    def run(self, stop: threading.Event) -> None:
        """Work until ``stop`` is set. A job in progress is always finished;
        the event is checked only between jobs."""

        logger.info("worker %s started with model %s", self.worker_id, self.model.name)
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Database or Redis trouble, not a bad job: the id stays on
                # this worker's processing list (see run_once) for recovery.
                logger.exception(
                    "worker %s hit an infrastructure error", self.worker_id
                )
                stop.wait(ERROR_BACKOFF_SECONDS)
        logger.info("worker %s stopped", self.worker_id)

    def run_once(self) -> Outcome | None:
        """Claim and handle at most one job. Returns None if none arrived."""

        raw_job_id = self.queue.claim(self.worker_id, self.poll_seconds)
        if raw_job_id is None:
            return None
        try:
            job_id = UUID(raw_job_id)
        except ValueError:
            logger.error("dropping malformed queue entry %r", raw_job_id)
            self.queue.ack(self.worker_id, raw_job_id)
            return Outcome.SKIPPED

        # If process() raises, the id is deliberately not acknowledged: it
        # stays on the processing list rather than being lost.
        outcome = self.process(job_id)
        self.queue.ack(self.worker_id, raw_job_id)
        return outcome

    def process(self, job_id: UUID) -> Outcome:
        input_path = self._start(job_id)
        if input_path is None:
            logger.info("job %s skipped: not queued", job_id)
            return Outcome.SKIPPED

        started = time.perf_counter()
        try:
            result_path = self._segment(job_id, input_path)
        except Exception as error:
            # Anything that goes wrong with this job's own data or the model
            # fails the job. Retries arrive in Milestone 4.
            message = f"{type(error).__name__}: {error}"[:MAX_ERROR_LENGTH]
            self._finish(job_id, JobStatus.FAILED, error=message)
            logger.warning("job %s failed: %s", job_id, message)
            return Outcome.FAILED

        self._finish(job_id, JobStatus.SUCCEEDED, result_path=result_path)
        logger.info(
            "job %s succeeded in %.0f ms",
            job_id,
            (time.perf_counter() - started) * 1000,
        )
        return Outcome.SUCCEEDED

    def _start(self, job_id: UUID) -> str | None:
        """Move the job from queued to running. Returns its input path, or None
        if it was not queued, so a second delivery of the same id does nothing."""

        with self.session_factory() as session:
            input_path = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                .values(
                    status=JobStatus.RUNNING,
                    started_at=func.now(),
                    model_name=self.model.name,
                )
                .returning(Job.input_path)
            ).scalar_one_or_none()
            session.commit()
        return input_path

    def _segment(self, job_id: UUID, input_path: str) -> str:
        with Image.open(self.storage_dir / input_path) as image:
            # No EXIF rotation: surgiseg-ml does not apply it either, and the
            # mask must line up with the pixels as stored.
            mask = self.model.predict(image.convert("RGB"))
        buffer = io.BytesIO()
        mask.save(buffer, format="PNG")
        result_path = f"masks/{job_id}.png"
        save_atomically(self.storage_dir, result_path, buffer.getvalue())
        return result_path

    def _finish(
        self,
        job_id: UUID,
        status: JobStatus,
        *,
        result_path: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.session_factory() as session:
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
                .values(
                    status=status,
                    result_path=result_path,
                    error=error,
                    finished_at=func.now(),
                )
            )
            session.commit()


def build_model(settings: Settings) -> SegmentationModel:
    return FakeModel(delay_seconds=settings.fake_model_delay_seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = Settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    worker = Worker(
        queue=JobQueue(Redis.from_url(settings.redis_url, decode_responses=True)),
        session_factory=sessionmaker(engine, expire_on_commit=False),
        storage_dir=settings.storage_dir,
        model=build_model(settings),
        worker_id=f"{socket.gethostname()}-{os.getpid()}",
        poll_seconds=settings.worker_poll_seconds,
    )

    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info(
            "received %s; finishing the current job, then stopping",
            signal.Signals(signum).name,
        )
        stop.set()

    # `docker compose stop` sends SIGTERM; Ctrl-C sends SIGINT.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    worker.run(stop)
    engine.dispose()


if __name__ == "__main__":
    main()
