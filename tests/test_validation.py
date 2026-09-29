import pytest
from imaging import noise, solid

from surgiserve.validation import InvalidUpload, display_filename, validate_image

LIMITS = {"min_side": 32, "max_side": 4096}


def rejection(data: bytes) -> InvalidUpload:
    with pytest.raises(InvalidUpload) as caught:
        validate_image(data, **LIMITS)
    return caught.value


def test_accepts_png_and_reports_its_dimensions() -> None:
    result = validate_image(solid((64, 48), "PNG"), **LIMITS)

    assert (result.format, result.extension) == ("PNG", "png")
    assert (result.width, result.height) == (64, 48)


def test_accepts_jpeg() -> None:
    result = validate_image(noise((128, 96), "JPEG"), **LIMITS)

    assert (result.format, result.extension) == ("JPEG", "jpg")
    assert (result.width, result.height) == (128, 96)


def test_accepts_grayscale_png() -> None:
    assert validate_image(solid(mode="L"), **LIMITS).format == "PNG"


def test_accepts_sizes_exactly_at_the_limits() -> None:
    assert validate_image(solid((4096, 32)), **LIMITS).width == 4096


def test_rejects_empty_file() -> None:
    assert rejection(b"").status_code == 422


def test_rejects_bytes_that_are_not_an_image() -> None:
    assert rejection(b"this is not an image at all").status_code == 415


def test_rejects_valid_images_in_other_formats() -> None:
    error = rejection(solid(image_format="GIF"))

    assert error.status_code == 415
    assert "GIF" in error.detail


def test_rejects_truncated_jpeg() -> None:
    data = noise(image_format="JPEG")

    assert rejection(data[: len(data) // 2]).status_code == 422


def test_rejects_jpeg_truncated_inside_its_header() -> None:
    # Cut short before the pixel data starts: this fails in Image.open, not in
    # the decode step, and must still be a 422 rather than a server error.
    assert rejection(noise(image_format="JPEG")[:500]).status_code == 422


def test_rejects_truncated_png() -> None:
    data = noise(image_format="PNG")

    assert rejection(data[: len(data) // 2]).status_code == 422


def test_rejects_an_oversized_side_before_decoding() -> None:
    error = rejection(solid((4097, 32)))

    assert error.status_code == 422
    assert "largest side" in error.detail


def test_rejects_an_undersized_side() -> None:
    error = rejection(solid((64, 31)))

    assert error.status_code == 422
    assert "smallest side" in error.detail


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("frame.png", "frame.png"),
        ("../../etc/passwd.png", "passwd.png"),
        ("C:\\Users\\someone\\frame.jpg", "frame.jpg"),
        ("bad\x00name\n.png", "badname.png"),
        ("folder/", None),
        ("", None),
        (None, None),
        ("a" * 300 + ".png", "a" * 255),
    ],
)
def test_display_filename(given: str | None, expected: str | None) -> None:
    assert display_filename(given) == expected
