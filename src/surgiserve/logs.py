"""Structured logs: one JSON object per line.

Every line has ``ts``, ``level``, ``service``, ``logger``, ``event`` and
``message``, plus whatever fields the call site or the surrounding context
adds. Context fields (``request_id`` in the API; ``worker_id``, ``job_id`` and
``attempt`` in the worker) are set once with ``log_context`` and then appear
on every line logged inside it, including lines from libraries. So a job can
be followed through both processes by its ``job_id``:

    docker compose logs api worker | grep 5e58b0c8

Call sites pass fields through ``extra``, always including an ``event`` name:

    logger.info("job succeeded", extra={"event": "job.succeeded", "duration_ms": 163})
"""

import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_context: ContextVar[Mapping[str, Any]] = ContextVar(
    "log_context", default=MappingProxyType({})
)

# Attributes every LogRecord has; anything else on a record is a field.
_RECORD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "log_context",
}


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add fields to every log record created inside the block."""

    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


_base_factory = logging.getLogRecordFactory()


def _record_with_context(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _base_factory(*args, **kwargs)
    # One namespaced attribute, not one attribute per field. The factory runs
    # before logging applies a call's ``extra``, and logging refuses to let
    # ``extra`` overwrite an existing attribute: a context field set directly
    # on the record would make ``extra={"job_id": ...}`` raise KeyError inside
    # a job. Found by test_an_explicit_field_wins_over_the_context.
    record.log_context = dict(_context.get())
    return record


def record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """A record's context and ``extra`` fields; ``extra`` wins on a clash."""

    fields = dict(getattr(record, "log_context", {}))
    fields.update(
        (key, value)
        for key, value in vars(record).items()
        if key not in _RECORD_ATTRIBUTES
    )
    return fields


def install_context_factory() -> None:
    """Make log records carry the current context's fields. Idempotent.

    Done with a record factory rather than a handler filter, so every handler
    sees the fields, pytest's included."""

    logging.setLogRecordFactory(_record_with_context)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "event": getattr(record, "event", None),
            "message": record.getMessage(),
        }
        for key, value in record_fields(record).items():
            if key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(
    service: str, *, log_format: str = "json", level: str = "INFO"
) -> None:
    """Send every logger's output to stdout, as JSON or as plain text."""

    install_context_factory()
    handler = logging.StreamHandler(sys.stdout)
    if log_format == "json":
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


request_logger = logging.getLogger("surgiserve.request")

# Accept a caller's request id only if it is short and plain, so it cannot
# inject anything into the logs.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestLogging:
    """One log line per request, with an id that is also returned to the
    client as ``X-Request-ID`` and attached to everything logged meanwhile.

    Health checks are logged at DEBUG: Docker polls them every few seconds.
    The path is logged without its query string."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        given = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = given if _SAFE_REQUEST_ID.match(given) else uuid.uuid4().hex
        status = 500  # if the app raises before responding

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        started = time.perf_counter()
        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                path = scope["path"]
                request_logger.log(
                    logging.DEBUG if path.startswith("/health") else logging.INFO,
                    "%s %s %d",
                    scope["method"],
                    path,
                    status,
                    extra={
                        "event": "http.request",
                        "method": scope["method"],
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
