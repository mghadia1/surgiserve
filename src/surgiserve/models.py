"""Database tables."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Enum, MetaData, String, func
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
    """The job lifecycle. In Milestone 1 nothing moves a job past ``queued``;
    the worker that does arrives in Milestone 2."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("width > 0 AND height > 0", name="positive_dimensions"),
        CheckConstraint("size_bytes > 0", name="positive_size"),
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
