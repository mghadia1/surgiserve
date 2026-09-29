"""Runtime configuration, read from ``SURGISERVE_*`` environment variables."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SURGISERVE_")

    # The defaults point at the Compose services as published on the host.
    database_url: str = (
        "postgresql+psycopg://surgiserve:surgiserve@localhost:5433/surgiserve"
    )
    redis_url: str = "redis://localhost:6380/0"
    # Uploaded images and masks live under this directory.
    storage_dir: Path = Path("var/storage")

    # Upload limits. Kvasir-Instrument frames are well inside all of these.
    max_upload_bytes: int = 10 * 1024 * 1024
    min_image_side: int = 32
    max_image_side: int = 4096

    # Worker. "fake" is the only model until Milestone 3.
    model: Literal["fake"] = "fake"
    # Simulated inference time for the fake model, so a job is visibly
    # "running" for a moment.
    fake_model_delay_seconds: float = 0.0
    # How long one wait on the queue lasts. The worker checks for a shutdown
    # request between waits, so this bounds how long stopping can take.
    worker_poll_seconds: float = 1.0
