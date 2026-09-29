"""Runtime configuration, read from ``SURGISERVE_*`` environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SURGISERVE_")

    # The default points at the Compose database as published on the host.
    database_url: str = (
        "postgresql+psycopg://surgiserve:surgiserve@localhost:5433/surgiserve"
    )
    # Uploaded images and (from Milestone 3) masks live under this directory.
    storage_dir: Path = Path("var/storage")

    # Upload limits. Kvasir-Instrument frames are well inside all of these.
    max_upload_bytes: int = 10 * 1024 * 1024
    min_image_side: int = 32
    max_image_side: int = 4096
