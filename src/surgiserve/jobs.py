"""The /jobs routes."""

import hashlib
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from .job_queue import JobQueue
from .models import Job, JobStatus
from .settings import Settings
from .storage import save_atomically
from .validation import InvalidUpload, display_filename, validate_image

router = APIRouter()


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_queue(request: Request) -> JobQueue:
    queue: JobQueue = request.app.state.queue
    return queue


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]


class JobInput(BaseModel):
    filename: str | None
    format: str
    width: int
    height: int
    size_bytes: int
    sha256: str


class JobOut(BaseModel):
    id: uuid.UUID
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    model: str | None
    error: str | None
    # Where to download the mask; set once the job has succeeded.
    mask_url: str | None
    input: JobInput

    @classmethod
    def from_job(cls, job: Job) -> "JobOut":
        return cls(
            id=job.id,
            status=job.status,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            model=job.model_name,
            error=job.error,
            mask_url=f"/jobs/{job.id}/mask" if job.result_path else None,
            input=JobInput(
                filename=job.original_filename,
                format=job.image_format,
                width=job.width,
                height=job.height,
                size_bytes=job.size_bytes,
                sha256=job.sha256,
            ),
        )


# Sync handlers: FastAPI runs them in a thread pool, so the blocking database
# and file calls below never stall the event loop.
@router.post("/jobs", status_code=202)
def create_job(
    image: UploadFile,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    queue: QueueDep,
) -> JobOut:
    """Accept an image and queue a segmentation job for it."""

    # Read one byte past the limit, so an oversized file is detected without
    # reading all of it. (Uploads that declare their size are already refused
    # by the Content-Length check before the body is read.)
    data = image.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, too_large_detail(settings.max_upload_bytes))
    try:
        validated = validate_image(
            data,
            min_side=settings.min_image_side,
            max_side=settings.max_image_side,
        )
    except InvalidUpload as error:
        raise HTTPException(error.status_code, error.detail) from None

    job_id = uuid.uuid4()
    relative_path = f"inputs/{job_id}.{validated.extension}"
    # File first, then row. If the insert fails the file is removed; if the
    # process dies between the two, an unreferenced file is left behind, which
    # is harmless. The reverse order could leave a job whose image is missing.
    stored = save_atomically(settings.storage_dir, relative_path, data)
    job = Job(
        id=job_id,
        status=JobStatus.QUEUED,
        input_path=relative_path,
        original_filename=display_filename(image.filename),
        image_format=validated.format,
        width=validated.width,
        height=validated.height,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    try:
        session.add(job)
        session.commit()
    except BaseException:
        session.rollback()
        stored.unlink(missing_ok=True)
        raise

    # Row first, then queue: a worker that pops the id must find the row. If
    # Redis is down the job could never run, so it is undone and the client
    # told to retry, rather than left "queued" forever.
    try:
        queue.enqueue(job_id)
    except RedisError:
        session.delete(job)
        session.commit()
        stored.unlink(missing_ok=True)
        raise HTTPException(503, "the job queue is unavailable; try again") from None
    session.refresh(job)

    response.headers["Location"] = f"/jobs/{job_id}"
    return JobOut.from_job(job)


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, session: SessionDep) -> JobOut:
    """Return a job's status, what was uploaded, and how it went."""

    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JobOut.from_job(job)


@router.get(
    "/jobs/{job_id}/mask",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/png": {}}, "description": "The mask as a PNG"},
        404: {"description": "No such job"},
        409: {"description": "The job has no mask (not finished, or failed)"},
    },
)
def get_mask(
    job_id: uuid.UUID, session: SessionDep, settings: SettingsDep
) -> FileResponse:
    """The job's mask: a grayscale PNG the size of the upload, 255 where the
    model found instrument and 0 elsewhere."""

    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status is not JobStatus.SUCCEEDED or job.result_path is None:
        raise HTTPException(409, f"job is {job.status}; it has no mask")
    storage = settings.storage_dir.resolve()
    path = (storage / job.result_path).resolve()
    # result_path is written by the worker, never by a client; this check is
    # a second line of defence against serving anything outside storage.
    if not path.is_relative_to(storage):
        raise HTTPException(500, "mask path is outside storage")
    return FileResponse(path, media_type="image/png", filename=f"{job_id}-mask.png")


def too_large_detail(limit: int) -> str:
    return f"upload exceeds the {limit}-byte limit"
