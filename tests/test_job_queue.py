import time
import uuid

import pytest
from fastapi import FastAPI

from surgiserve.job_queue import JobQueue


@pytest.fixture
def queue(app: FastAPI) -> JobQueue:
    job_queue: JobQueue = app.state.queue
    return job_queue


def test_jobs_are_claimed_oldest_first(queue: JobQueue) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    queue.enqueue(first)
    queue.enqueue(second)

    assert queue.waiting() == [str(first), str(second)]
    assert queue.claim("w1", 0.1) == str(first)
    assert queue.claim("w1", 0.1) == str(second)


def test_a_claimed_id_moves_to_the_processing_list_until_acked(
    queue: JobQueue,
) -> None:
    job_id = uuid.uuid4()
    queue.enqueue(job_id)

    claimed = queue.claim("w1", 0.1)

    assert claimed == str(job_id)
    assert queue.waiting() == []
    assert queue.processing("w1") == [str(job_id)]

    queue.ack("w1", claimed)

    assert queue.processing("w1") == []


def test_each_worker_holds_only_what_it_claimed(queue: JobQueue) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    queue.enqueue(first)
    queue.enqueue(second)

    queue.claim("w1", 0.1)
    queue.claim("w2", 0.1)

    assert queue.processing("w1") == [str(first)]
    assert queue.processing("w2") == [str(second)]


def test_claim_on_an_empty_queue_times_out(queue: JobQueue) -> None:
    started = time.monotonic()

    assert queue.claim("w1", 0.2) is None
    assert time.monotonic() - started < 2
