"""A small job queue on Redis lists, written out so every step is visible.

Producers push job ids onto the left of ``surgiserve:queue``. A worker claims
one with ``BLMOVE``, which in a single atomic step takes the oldest id off the
right of the queue and puts it on that worker's own processing list. The id is
removed from the processing list (acknowledged) only after the job's outcome
is saved in Postgres.

So an id is always in exactly one place: waiting in the queue, or held by one
worker. If a worker dies mid-job, its id stays on its processing list, where a
recovery step can find it and put it back (Milestone 4). The queue carries only
ids; Postgres stays the record of what each job is and what happened to it.
"""

from typing import cast
from uuid import UUID

from redis import Redis

QUEUE_KEY = "surgiserve:queue"
PROCESSING_KEY_PREFIX = "surgiserve:processing:"


def processing_key(worker_id: str) -> str:
    return f"{PROCESSING_KEY_PREFIX}{worker_id}"


class JobQueue:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    def enqueue(self, job_id: UUID) -> None:
        self.redis.lpush(QUEUE_KEY, str(job_id))

    def claim(self, worker_id: str, timeout_seconds: float) -> str | None:
        """Wait up to ``timeout_seconds`` for a job id and move it to this
        worker's processing list. Returns the raw id, or None on timeout."""

        claimed = self.redis.blmove(
            QUEUE_KEY,
            processing_key(worker_id),
            # Redis accepts fractional seconds here; redis-py's annotation
            # says int, which is narrower than the command.
            timeout_seconds,  # type: ignore[arg-type]
            "RIGHT",
            "LEFT",
        )
        return cast(str | None, claimed)

    def ack(self, worker_id: str, raw_job_id: str) -> None:
        """Drop a finished job id from the worker's processing list."""

        self.redis.lrem(processing_key(worker_id), 1, raw_job_id)

    def waiting(self) -> list[str]:
        """Ids still in the queue, oldest first."""

        return list(reversed(cast(list[str], self.redis.lrange(QUEUE_KEY, 0, -1))))

    def processing(self, worker_id: str) -> list[str]:
        return cast(list[str], self.redis.lrange(processing_key(worker_id), 0, -1))
