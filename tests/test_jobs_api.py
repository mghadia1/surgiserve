import hashlib
import random
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from imaging import noise, solid
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from surgiserve.settings import Settings


def job_count(app: FastAPI) -> int:
    with app.state.engine.connect() as connection:
        count: int = connection.scalar(text("SELECT count(*) FROM jobs"))
    return count


def stored_files(settings: Settings) -> list[Path]:
    inputs = settings.storage_dir / "inputs"
    return sorted(inputs.iterdir()) if inputs.exists() else []


def test_create_job_queues_it_and_stores_the_image(
    client: TestClient, settings: Settings
) -> None:
    data = solid((64, 48), "PNG")

    response = client.post("/jobs", files={"image": ("frame.png", data, "image/png")})

    assert response.status_code == 202
    body = response.json()
    job_id = uuid.UUID(body["id"])
    assert response.headers["location"] == f"/jobs/{job_id}"
    assert body["status"] == "queued"
    assert body["input"] == {
        "filename": "frame.png",
        "format": "PNG",
        "width": 64,
        "height": 48,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    stored = settings.storage_dir / "inputs" / f"{job_id}.png"
    assert stored.read_bytes() == data


def test_get_job_returns_what_create_returned(client: TestClient) -> None:
    created = client.post(
        "/jobs", files={"image": ("frame.jpg", noise(), "image/jpeg")}
    ).json()

    response = client.get(f"/jobs/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_format_comes_from_the_bytes_not_the_name_or_content_type(
    client: TestClient, settings: Settings
) -> None:
    response = client.post(
        "/jobs", files={"image": ("photo.jpg", solid(), "image/jpeg")}
    )

    assert response.status_code == 202
    assert response.json()["input"]["format"] == "PNG"
    [stored] = stored_files(settings)
    assert stored.suffix == ".png"


def test_client_file_name_never_reaches_the_file_system(
    client: TestClient, settings: Settings
) -> None:
    response = client.post(
        "/jobs", files={"image": ("../../../evil.png", solid(), "image/png")}
    )

    body = response.json()
    assert body["input"]["filename"] == "evil.png"
    assert stored_files(settings) == [
        settings.storage_dir / "inputs" / f"{body['id']}.png"
    ]


@pytest.mark.parametrize(
    ("data", "status"),
    [
        (solid(image_format="GIF"), 415),
        (b"not an image", 415),
        (noise()[:500], 422),
        (solid((16, 16)), 422),
        (b"", 422),
    ],
    ids=["gif", "garbage", "truncated", "too-small", "empty"],
)
def test_rejected_uploads_leave_no_job_and_no_file(
    client: TestClient,
    app: FastAPI,
    settings: Settings,
    data: bytes,
    status: int,
) -> None:
    response = client.post("/jobs", files={"image": ("frame", data, "image/png")})

    assert response.status_code == status
    assert "detail" in response.json()
    assert job_count(app) == 0
    assert stored_files(settings) == []


def test_missing_image_field_is_422(client: TestClient) -> None:
    assert client.post("/jobs").status_code == 422


def test_declared_oversized_body_is_refused_before_it_is_read(
    settings: Settings, database_url: str
) -> None:
    from surgiserve.app import create_app

    small = settings.model_copy(update={"max_upload_bytes": 1_000})
    client = TestClient(create_app(small))
    big = random.Random(1).randbytes(200_000)  # well past limit + multipart slack

    response = client.post("/jobs", files={"image": ("big.png", big, "image/png")})

    assert response.status_code == 413
    assert "1000-byte limit" in response.json()["detail"]
    assert stored_files(small) == []


def test_oversized_file_inside_the_body_allowance_is_still_refused(
    settings: Settings, database_url: str
) -> None:
    # The body is under the Content-Length allowance (limit + 64 KiB for
    # multipart framing), so it reaches the handler, whose own check fires.
    from surgiserve.app import create_app

    small = settings.model_copy(update={"max_upload_bytes": 1_000})
    client = TestClient(create_app(small))

    response = client.post(
        "/jobs", files={"image": ("big.png", b"x" * 5_000, "image/png")}
    )

    assert response.status_code == 413
    assert stored_files(small) == []


def test_database_failure_removes_the_stored_file(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self: Session) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(Session, "commit", fail)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/jobs", files={"image": ("frame.png", solid(), "image/png")}
    )

    assert response.status_code == 500
    assert stored_files(settings) == []


def test_unknown_job_is_404(client: TestClient) -> None:
    response = client.get(f"/jobs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "job not found"}


def test_malformed_job_id_is_422(client: TestClient) -> None:
    assert client.get("/jobs/not-a-uuid").status_code == 422


def test_database_rejects_an_unknown_status(app: FastAPI) -> None:
    insert = text(
        "INSERT INTO jobs (id, status, input_path, image_format, width, height, "
        "size_bytes, sha256) VALUES (:id, 'bogus', 'x', 'PNG', 1, 1, 1, 'x')"
    )
    with pytest.raises(IntegrityError, match="ck_jobs_status"):
        with app.state.engine.begin() as connection:
            connection.execute(insert, {"id": uuid.uuid4()})
