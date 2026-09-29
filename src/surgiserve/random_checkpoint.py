"""Write a randomly initialised checkpoint with the frozen run's architecture.

The weights are meaningless. It lets tests and CI run the real inference
path (loading, preprocessing, TTA, thresholding) where the real 57 MB
checkpoint is not available.

    python -m surgiserve.random_checkpoint OUTPUT_DIR

prints the two files' SHA-256 values as JSON.
"""

import json
import sys
from pathlib import Path

import torch
from surgiseg.config import TrainConfig
from surgiseg.model import build_model

from .surgiseg_model import sha256_of

# The frozen run's config.json, field for field.
FROZEN_CONFIG = {
    "image_size": 128,
    "base_channels": 16,
    "batch_size": 8,
    "epochs": 30,
    "learning_rate": 0.001,
    "weight_decay": 1e-05,
    "threshold": 0.5,
    "seed": 42,
    "num_workers": 0,
    "device": "cpu",
    "preprocessing": "square",
    "architecture": "resnet18-unet",
    "encoder_weights": "imagenet1k-v1",
    "augmentation": "horizontal-flip",
}


def write(directory: Path, *, seed: int = 0) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.json"
    checkpoint_path = directory / "best-model.pt"
    config_path.write_text(json.dumps(FROZEN_CONFIG, indent=2) + "\n")

    torch.manual_seed(seed)
    network = build_model(TrainConfig(**FROZEN_CONFIG), initialize_encoder=False)
    torch.save({"model_state": network.state_dict()}, checkpoint_path)
    return {
        "checkpoint_sha256": sha256_of(checkpoint_path),
        "config_sha256": sha256_of(config_path),
    }


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m surgiserve.random_checkpoint OUTPUT_DIR")
    print(json.dumps(write(Path(sys.argv[1]))))


if __name__ == "__main__":
    main()
