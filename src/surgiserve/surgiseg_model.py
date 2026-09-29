"""The real model: surgiseg-ml's ResNet-18 U-Net.

The network and its config class are imported from surgiseg-ml itself. What
this module adds is the inference path, and that path must match the one that
produced the benchmark (``surgiseg.evaluation._predict_probabilities``) step
for step, or the masks stop meaning what the benchmark says they mean:

1. RGB, float, divided by 255. No EXIF rotation, as in surgiseg-ml.
2. Stretch to image_size x image_size, bilinear, ``align_corners=False``.
3. Sigmoid of the network output; with flip TTA, averaged with the
   prediction for the horizontally flipped image, flipped back.
4. Threshold with ``>=``.

``tests/test_surgiseg_model.py`` checks steps 1-3 against surgiseg-ml's own
function, and, where the weights and data exist, against the probabilities
saved by the frozen benchmark run.

Loading goes through ``build_model(config, initialize_encoder=False)``. That
matters: ImageNet normalisation happens inside the network only when it is
built with ``normalize_for_imagenet=True``, which build_model derives from the
config. A network built by hand with defaults loads the same weights without
any error and returns wrong masks.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from surgiseg.config import TrainConfig
from surgiseg.model import build_model
from torch.nn import functional


class ChecksumMismatch(RuntimeError):
    """A model file is not the one the service was configured to run."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path: Path, expected_sha256: str) -> None:
    actual = sha256_of(path)
    if actual != expected_sha256:
        raise ChecksumMismatch(
            f"{path} has SHA-256 {actual}, expected {expected_sha256}"
        )


def load_config(path: Path) -> TrainConfig:
    config = TrainConfig(**json.loads(path.read_text()))
    config.validate()
    if config.preprocessing != "square":
        # Letterboxing would also need the padding removed from the mask;
        # only the frozen run's "square" mode is implemented.
        raise ValueError(f"unsupported preprocessing: {config.preprocessing}")
    return config


class SurgisegModel:
    def __init__(
        self,
        network: torch.nn.Module,
        config: TrainConfig,
        *,
        threshold: float,
        flip_tta: bool,
        name: str,
    ) -> None:
        self.network = network.eval()
        self.config = config
        self.threshold = threshold
        self.flip_tta = flip_tta
        self.name = name

    @classmethod
    def load(
        cls,
        checkpoint_path: Path,
        config_path: Path,
        *,
        checkpoint_sha256: str,
        config_sha256: str,
        threshold: float,
        flip_tta: bool,
    ) -> "SurgisegModel":
        """Verify both files, then build the network and load the weights."""

        verify(checkpoint_path, checkpoint_sha256)
        verify(config_path, config_sha256)
        config = load_config(config_path)
        network: torch.nn.Module = build_model(config, initialize_encoder=False)
        checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
        # strict (the default): every weight must match a parameter exactly.
        network.load_state_dict(checkpoint["model_state"])
        return cls(
            network,
            config,
            threshold=threshold,
            flip_tta=flip_tta,
            name=f"surgiseg-{config.architecture}@{checkpoint_sha256[:12]}",
        )

    def preprocess(self, image: Image.Image) -> torch.Tensor:
        """A 1 x 3 x size x size batch, exactly as surgiseg-ml builds it."""

        array = np.asarray(image.convert("RGB"))
        tensor = torch.from_numpy(np.array(array, copy=True)).permute(2, 0, 1)
        tensor = tensor.float() / 255.0
        size = self.config.image_size
        return functional.interpolate(
            tensor.unsqueeze(0), size=(size, size), mode="bilinear", align_corners=False
        )

    def probabilities(self, image: Image.Image) -> torch.Tensor:
        """Instrument probability per pixel, at the network's input size."""

        batch = self.preprocess(image)
        with torch.inference_mode():
            probability = torch.sigmoid(self.network(batch))
            if self.flip_tta:
                flipped = self.network(torch.flip(batch, dims=(3,)))
                probability = (
                    probability + torch.flip(torch.sigmoid(flipped), dims=(3,))
                ) / 2.0
        return probability[0, 0]

    def predict(self, image: Image.Image) -> Image.Image:
        """A mode-"L" mask at the input's own size: 255 instrument, 0 not.

        Probabilities are resized back to the original size before
        thresholding. That step is this service's; the benchmark scored masks
        at the network's input size.
        """

        width, height = image.size
        resized = functional.interpolate(
            self.probabilities(image)[None, None],
            size=(height, width),
            mode="bilinear",
            align_corners=False,
        )[0, 0]
        mask = (resized >= self.threshold).to(torch.uint8) * 255
        return Image.fromarray(mask.numpy())
