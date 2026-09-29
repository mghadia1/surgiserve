"""Helpers shared by the worker tests."""

import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from surgiserve.segmentation import FakeModel, SegmentationModel
from surgiserve.settings import Settings
from surgiserve.worker import Policy, Worker

WORKER_ID = "test-worker"


def half_bright_png() -> bytes:
    image = Image.new("RGB", (64, 48), (20, 20, 20))
    image.paste((240, 240, 240), (32, 0, 64, 48))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_worker(
    app: FastAPI,
    settings: Settings,
    model: SegmentationModel | None = None,
    *,
    policy: Policy | None = None,
    worker_id: str = WORKER_ID,
) -> Worker:
    return Worker(
        queue=app.state.queue,
        session_factory=app.state.session_factory,
        storage_dir=settings.storage_dir,
        model=model or FakeModel(),
        worker_id=worker_id,
        poll_seconds=0.1,
        policy=policy,
    )


def submit(client: TestClient) -> str:
    response = client.post(
        "/jobs", files={"image": ("frame.png", half_bright_png(), "image/png")}
    )
    assert response.status_code == 202
    job_id: str = response.json()["id"]
    return job_id
