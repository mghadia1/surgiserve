"""Operator commands for jobs that did not succeed.

    python -m surgiserve.admin dead-letters [--limit N]
    python -m surgiserve.admin requeue JOB_ID [JOB_ID ...]

A command line rather than HTTP routes: the API has no authentication, and
requeueing is an operator's decision, not a client's.
"""

import argparse
import uuid
from collections.abc import Sequence

from redis import Redis
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from .job_queue import JobQueue
from .models import Job, JobStatus
from .settings import Settings

REQUEUEABLE = (JobStatus.DEAD_LETTERED, JobStatus.FAILED)


def dead_letters(session_factory: sessionmaker[Session], limit: int = 50) -> list[Job]:
    """Dead-lettered jobs, most recent first."""

    with session_factory() as session:
        return list(
            session.scalars(
                select(Job)
                .where(Job.status == JobStatus.DEAD_LETTERED)
                .order_by(Job.updated_at.desc())
                .limit(limit)
            )
        )


def requeue(
    session_factory: sessionmaker[Session],
    queue: JobQueue,
    job_ids: Sequence[uuid.UUID],
) -> list[uuid.UUID]:
    """Give dead-lettered or failed jobs a fresh set of attempts.

    Returns the ids actually requeued; others (unknown, or not in a
    requeueable state) are left alone. If the enqueue step fails, the jobs are
    already queued in the database and the stranded-job sweep picks them up.
    """

    with session_factory() as session:
        requeued = list(
            session.scalars(
                update(Job)
                .where(Job.id.in_(job_ids), Job.status.in_(REQUEUEABLE))
                .values(
                    status=JobStatus.QUEUED,
                    attempts=0,
                    error=None,
                    result_path=None,
                    started_at=None,
                    finished_at=None,
                )
                .returning(Job.id)
            )
        )
        session.commit()
    for job_id in requeued:
        queue.enqueue(job_id)
    return requeued


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m surgiserve.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("dead-letters", help="list dead-lettered jobs")
    listing.add_argument("--limit", type=int, default=50)
    requeueing = commands.add_parser(
        "requeue", help="requeue dead-lettered or failed jobs"
    )
    requeueing.add_argument("job_ids", nargs="+", type=uuid.UUID)
    arguments = parser.parse_args(argv)

    settings = Settings()
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    if arguments.command == "dead-letters":
        jobs = dead_letters(session_factory, arguments.limit)
        for job in jobs:
            print(
                f"{job.id}  attempts={job.attempts}  {job.updated_at:%Y-%m-%d %H:%M:%S}"
            )
            print(f"    {job.error}")
        print(f"{len(jobs)} dead-lettered job(s)")
    else:
        queue = JobQueue(Redis.from_url(settings.redis_url, decode_responses=True))
        done = requeue(session_factory, queue, arguments.job_ids)
        for job_id in done:
            print(f"requeued {job_id}")
        skipped = set(arguments.job_ids) - set(done)
        for job_id in sorted(skipped, key=str):
            print(f"skipped {job_id}: unknown, or not dead-lettered or failed")
    engine.dispose()


if __name__ == "__main__":
    main()
