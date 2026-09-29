"""Runtime configuration, read from ``SURGISERVE_*`` environment variables."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# The frozen surgiseg-ml run of 2026-08-03, as recorded in its
# outputs/kvasir-frozen-final-2026-08-03/official-test-benchmark.json.
FROZEN_CHECKPOINT_SHA256 = (
    "5c23082074017c1d7833712defb45c68c60de2394fbb4eded7920c7e17253dda"
)
FROZEN_CONFIG_SHA256 = (
    "44230e3cce5e099d432c984ff7c84b057a72df1e6ac8ea2b6cc936c819d9a66c"
)
# The threshold the benchmark was scored at. Not the 0.5 in that run's
# config.json, which is the training default.
FROZEN_THRESHOLD = 0.47


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

    # Worker model. "surgiseg" is the real network; "fake" is a brightness
    # threshold for exercising the pipeline without weights.
    model: Literal["surgiseg", "fake"] = "surgiseg"
    model_checkpoint_path: Path = Path("weights/best-model.pt")
    model_config_path: Path = Path("weights/config.json")
    # The worker refuses to start unless both files hash to these values.
    model_checkpoint_sha256: str = FROZEN_CHECKPOINT_SHA256
    model_config_sha256: str = FROZEN_CONFIG_SHA256
    model_threshold: float = FROZEN_THRESHOLD
    model_flip_tta: bool = True
    # Simulated inference time for the fake model, so a job is visibly
    # "running" for a moment.
    fake_model_delay_seconds: float = 0.0
    # How long one wait on the queue lasts. The worker checks for a shutdown
    # request between waits, so this bounds how long stopping can take.
    worker_poll_seconds: float = 1.0
