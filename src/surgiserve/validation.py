"""Upload validation, independent of the web framework.

The format is decided by decoding the bytes, never by the file name or the
Content-Type the client claims.
"""

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

# Pillow format name -> file extension used in storage.
ALLOWED_FORMATS = {"PNG": "png", "JPEG": "jpg"}


class InvalidUpload(Exception):
    """An upload the API rejects. Carries the HTTP status to answer with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ValidatedImage:
    format: str
    extension: str
    width: int
    height: int


def validate_image(data: bytes, *, min_side: int, max_side: int) -> ValidatedImage:
    """Check that ``data`` is a complete PNG or JPEG of an acceptable size."""

    if not data:
        raise InvalidUpload(422, "the uploaded file is empty")
    try:
        # Image.open only reads the header; nothing is decoded yet.
        image = Image.open(BytesIO(data))
    except UnidentifiedImageError:
        raise InvalidUpload(415, "not a PNG or JPEG image") from None
    except (OSError, SyntaxError, ValueError):
        # Recognised, but the header itself is cut short or malformed.
        # (UnidentifiedImageError is an OSError, so it must be caught first.)
        raise InvalidUpload(422, "image data is corrupt or truncated") from None

    with image:
        image_format = image.format or "unknown"
        if image_format not in ALLOWED_FORMATS:
            raise InvalidUpload(
                415, f"unsupported image format {image_format}; send PNG or JPEG"
            )
        width, height = image.size
        # Checked from the header before decoding, so a small file that claims
        # huge dimensions is rejected without allocating memory for it.
        if max(width, height) > max_side:
            raise InvalidUpload(
                422,
                f"image is {width}x{height}; the largest side allowed is {max_side}",
            )
        if min(width, height) < min_side:
            raise InvalidUpload(
                422,
                f"image is {width}x{height}; the smallest side allowed is {min_side}",
            )
        try:
            # Decode every pixel, so truncated or corrupt files fail here rather
            # than in the worker.
            image.load()
        except (OSError, SyntaxError, ValueError):
            raise InvalidUpload(422, "image data is corrupt or truncated") from None

    return ValidatedImage(
        format=image_format,
        extension=ALLOWED_FORMATS[image_format],
        width=width,
        height=height,
    )


def display_filename(filename: str | None) -> str | None:
    """Reduce a client-supplied file name to something safe to show back.

    It is stored for display only; storage paths never use it.
    """

    if not filename:
        return None
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in base if character.isprintable())
    return cleaned[:255] or None
