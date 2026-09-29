"""Database tables."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Enum, MetaData, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can refer to them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class JobStatus(StrEnum):
    """The job lifecycle.

    queued -> running -> succeeded
                      -> failed         (a problem retrying cannot fix)
                      -> queued         (retry after a backoff)
                      -> dead_lettered  (out of attempts)

    Every transition is a conditional UPDATE on the current status (and, for
    finishing, on the attempt number), so a job id delivered twice cannot run
    twice and a worker presumed dead cannot overwrite a newer attempt."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("width > 0 AND height > 0", name="positive_dimensions"),
        CheckConstraint("size_bytes > 0", name="positive_size"),
        CheckConstraint("attempts >= 0", name="non_negative_attempts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # Stored as VARCHAR plus a CHECK constraint rather than a native Postgres
    # enum: adding a status later is a constraint swap, not an ALTER TYPE.
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda statuses: [status.value for status in statuses],
        )
    )

    # Path of the uploaded image relative to the storage directory. The name
    # is derived from the job id, never from anything the client sent.
    input_path: Mapped[str] = mapped_column(String(255))
    # Display only. Never used to build a path.
    original_filename: Mapped[str | None] = mapped_column(String(255))
    image_format: Mapped[str] = mapped_column(String(8))
    width: Mapped[int]
    height: Mapped[int]
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))

    # Filled in by the worker. Counts starts, including ones cut short by a
    # worker dying, so a job that keeps killing workers runs out of attempts.
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    model_name: Mapped[str | None] = mapped_column(String(64))
    # Mask path relative to the storage directory; set only on success.
    result_path: Mapped[str | None] = mapped_column(String(255))
    # Why the last attempt failed; kept while a retry is pending.
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
