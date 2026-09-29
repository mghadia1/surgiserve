"""Build small test images in memory."""

import random
from io import BytesIO

from PIL import Image


def solid(
    size: tuple[int, int] = (64, 48), image_format: str = "PNG", mode: str = "RGB"
) -> bytes:
    color: int | tuple[int, int, int] = 128 if mode == "L" else (200, 40, 40)
    buffer = BytesIO()
    Image.new(mode, size, color).save(buffer, format=image_format)
    return buffer.getvalue()


def noise(size: tuple[int, int] = (128, 128), image_format: str = "JPEG") -> bytes:
    """Random pixels, so the encoded file is mostly pixel data.

    Truncating it therefore cuts into the pixels, not just trailing metadata.
    """

    pixels = random.Random(0).randbytes(size[0] * size[1] * 3)
    buffer = BytesIO()
    Image.frombytes("RGB", size, pixels).save(buffer, format=image_format)
    return buffer.getvalue()
