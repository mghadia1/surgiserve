"""The interface the worker runs models through, and a fake model.

The real one, surgiseg-ml's network, is in ``surgiseg_model.py``.
"""

import time
from typing import Protocol

from PIL import Image


class SegmentationModel(Protocol):
    # Recorded on each job, so every stored mask says what produced it.
    name: str

    def predict(self, image: Image.Image) -> Image.Image:
        """Return a mode-"L" mask the size of ``image``: 255 instrument, 0 not."""
        ...


class FakeModel:
    """Stands in for the network: marks bright pixels as "instrument".

    It exercises the queue, worker and storage without model weights. Its
    output means nothing about instruments.
    """

    name = "fake-brightness-threshold"

    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds

    def predict(self, image: Image.Image) -> Image.Image:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return image.convert("L").point(lambda value: 255 if value > 127 else 0)
