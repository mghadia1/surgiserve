import json
import logging
import sys
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from imaging import solid
from support import make_worker, submit

from surgiserve.app import create_app
from surgiserve.logs import JsonFormatter, log_context, record_fields
from surgiserve.settings import Settings


def logged(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, object]]:
    """The fields of each captured record with this event, as a dict."""

    return [
        record_fields(record)
        for record in caplog.records
        if getattr(record, "event", None) == event
    ]


def format_record(record: logging.LogRecord) -> dict[str, object]:
    line = JsonFormatter("test").format(record)
    assert "\n" not in line  # one line per record
    parsed: dict[str, object] = json.loads(line)
    return parsed


def make_record(message: str = "hello", **extra: object) -> logging.LogRecord:
    return logging.getLogger("surgiserve.test").makeRecord(
        "surgiserve.test", logging.INFO, __file__, 1, message, (), None, extra=extra
    )


def test_json_lines_have_the_standard_fields_and_extras() -> None:
    entry = format_record(make_record(event="thing.happened", duration_ms=12.5))

    assert entry["level"] == "info"
    assert entry["service"] == "test"
    assert entry["logger"] == "surgiserve.test"
    assert entry["event"] == "thing.happened"
    assert entry["message"] == "hello"
    assert entry["duration_ms"] == 12.5
    assert str(entry["ts"]).endswith("Z")


def test_context_fields_appear_on_records_made_inside_it() -> None:
    with log_context(request_id="r1"), log_context(job_id="j1"):
        inner = format_record(make_record())
    outer = format_record(make_record())

    assert (inner["request_id"], inner["job_id"]) == ("r1", "j1")
    assert "request_id" not in outer and "job_id" not in outer


def test_an_explicit_field_wins_over_the_context() -> None:
    with log_context(job_id="from-context"):
        entry = format_record(make_record(job_id="explicit"))

    assert entry["job_id"] == "explicit"


def test_exceptions_are_included_as_one_field() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        record = logging.getLogger("t").makeRecord(
            "t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )

    entry = format_record(record)

    assert "ValueError: bad" in str(entry["exception"])


def test_values_json_cannot_encode_are_stringified() -> None:
    job_id = uuid.uuid4()

    assert format_record(make_record(job_id=job_id))["job_id"] == str(job_id)


def test_every_response_carries_a_request_id() -> None:
    client = TestClient(create_app())

    generated = client.get("/health").headers["x-request-id"]
    echoed = client.get("/health", headers={"X-Request-ID": "trace-123"})
    unsafe = client.get("/health", headers={"X-Request-ID": "bad id\nforged: 1"})

    assert len(generated) == 32
    assert echoed.headers["x-request-id"] == "trace-123"
    assert unsafe.headers["x-request-id"] != "bad id\nforged: 1"


def test_each_request_is_logged_once_with_its_outcome(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    response = client.post(
        "/jobs", files={"image": ("frame.png", solid(), "image/png")}
    )

    [request] = logged(caplog, "http.request")
    assert (request["method"], request["path"], request["status"]) == (
        "POST",
        "/jobs",
        202,
    )
    assert request["request_id"] == response.headers["x-request-id"]
    # The job's creation is tied to the request that made it.
    [created] = logged(caplog, "job.created")
    assert created["request_id"] == response.headers["x-request-id"]
    assert created["job_id"] == response.json()["id"]


def test_refused_uploads_are_logged_too(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    client.post(
        "/jobs", files={"image": ("x.gif", solid(image_format="GIF"), "image/gif")}
    )

    assert logged(caplog, "upload.rejected")
    [request] = logged(caplog, "http.request")
    assert request["status"] == 415


def test_health_checks_are_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    TestClient(create_app()).get("/health")

    assert logged(caplog, "http.request") == []


def test_worker_logs_carry_the_job_and_attempt(
    client: TestClient,
    app: FastAPI,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    job_id = submit(client)

    make_worker(app, settings).run_once()

    [succeeded] = logged(caplog, "job.succeeded")
    assert succeeded["job_id"] == job_id
    assert succeeded["attempt"] == 1
    assert isinstance(succeeded["duration_ms"], float)
