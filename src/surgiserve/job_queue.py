"""A small job queue on Redis, written out so every step is visible.

Producers push job ids onto the left of ``surgiserve:queue``. A worker claims
one with ``BLMOVE``, which in a single atomic step takes the oldest id off the
right of the queue and puts it on that worker's own processing list. The id is
removed from the processing list (acknowledged) only after the job's outcome
is saved in Postgres.

Around that core:

- **Retries.** A job to be retried later waits in the sorted set
  ``surgiserve:delayed``, scored by the Unix time it becomes due. Workers move
  due ids back onto the queue with one atomic script.
- **Liveness.** Each worker keeps ``surgiserve:worker:<id>`` alive with a
  short TTL. A processing list whose worker key has expired belongs to a dead
  worker; its ids are put back on the queue, one atomic script per id.

The queue carries only ids. Postgres stays the record of what each job is and
what happened to it, including which jobs are dead-lettered.
"""

from collections.abc import Iterator
from typing import cast
from uuid import UUID

from redis import Redis

QUEUE_KEY = "surgiserve:queue"
DELAYED_KEY = "surgiserve:delayed"
PROCESSING_KEY_PREFIX = "surgiserve:processing:"
HEARTBEAT_KEY_PREFIX = "surgiserve:worker:"

# Move every id whose due time has passed from the delayed set to the queue.
# One script, so an id is never in both places or in neither.
PROMOTE_DUE = """
local now = redis.call('TIME')
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now[1] + now[2] / 1e6,
                       'LIMIT', 0, tonumber(ARGV[1]))
for _, id in ipairs(due) do
  redis.call('ZREM', KEYS[1], id)
  redis.call('LPUSH', KEYS[2], id)
end
return #due
"""

# Move one id from a (dead) worker's processing list back to the queue, but
# only if it is still there: if two workers reap the same list at once, the id
# is moved once.
RETURN_TO_QUEUE = """
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 1 then
  redis.call('LPUSH', KEYS[2], ARGV[1])
  return 1
end
return 0
"""


def processing_key(worker_id: str) -> str:
    return f"{PROCESSING_KEY_PREFIX}{worker_id}"


def heartbeat_key(worker_id: str) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}{worker_id}"


class JobQueue:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._promote_due = redis.register_script(PROMOTE_DUE)
        self._return_to_queue = redis.register_script(RETURN_TO_QUEUE)

    # --- the core -----------------------------------------------------------

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

    # --- retries ------------------------------------------------------------

    def schedule(self, job_id: UUID, delay_seconds: float) -> None:
        """Put the id back on the queue after ``delay_seconds``."""

        seconds, microseconds = self.redis.time()
        due = seconds + microseconds / 1e6 + delay_seconds
        self.redis.zadd(DELAYED_KEY, {str(job_id): due})

    def promote_due(self, limit: int = 100) -> int:
        """Move up to ``limit`` due retries onto the queue; returns how many.

        Due times are compared with Redis's clock, not the caller's, so
        workers with drifting clocks agree."""

        return cast(int, self._promote_due(keys=[DELAYED_KEY, QUEUE_KEY], args=[limit]))

    # --- liveness and recovery ----------------------------------------------

    def beat(self, worker_id: str, ttl_seconds: float) -> None:
        """Mark the worker alive for the next ``ttl_seconds``."""

        self.redis.set(heartbeat_key(worker_id), "alive", px=int(ttl_seconds * 1000))

    def forget(self, worker_id: str) -> None:
        self.redis.delete(heartbeat_key(worker_id))

    def is_alive(self, worker_id: str) -> bool:
        return bool(self.redis.exists(heartbeat_key(worker_id)))

    def workers_holding_jobs(self) -> Iterator[str]:
        """Ids of workers with a non-empty processing list (Redis deletes a
        list when it empties, so every key found holds at least one id)."""

        for key in self.redis.scan_iter(match=f"{PROCESSING_KEY_PREFIX}*"):
            yield cast(str, key).removeprefix(PROCESSING_KEY_PREFIX)

    def return_to_queue(self, worker_id: str, raw_job_id: str) -> bool:
        """Move one id from a worker's processing list back to the queue.
        False if it was no longer there (another worker got to it first)."""

        moved = self._return_to_queue(
            keys=[processing_key(worker_id), QUEUE_KEY], args=[raw_job_id]
        )
        return bool(moved)

    def drop_from_processing(self, worker_id: str, raw_job_id: str) -> None:
        self.ack(worker_id, raw_job_id)

    def is_waiting(self, job_id: UUID) -> bool:
        """Whether the id is on the queue or scheduled for a retry.

        LPOS scans the list, so this is O(queue length); fine for the sweeper,
        which checks a handful of ids now and then."""

        raw = str(job_id)
        return (
            self.redis.lpos(QUEUE_KEY, raw) is not None
            or self.redis.zscore(DELAYED_KEY, raw) is not None
        )

    # --- inspection ---------------------------------------------------------

    def waiting(self) -> list[str]:
        """Ids still in the queue, oldest first."""

        return list(reversed(cast(list[str], self.redis.lrange(QUEUE_KEY, 0, -1))))

    def delayed(self) -> dict[str, float]:
        """Ids waiting to be retried, with the Unix time each becomes due."""

        entries = cast(
            list[tuple[str, float]],
            self.redis.zrange(DELAYED_KEY, 0, -1, withscores=True),
        )
        return dict(entries)

    def processing(self, worker_id: str) -> list[str]:
        return cast(list[str], self.redis.lrange(processing_key(worker_id), 0, -1))
