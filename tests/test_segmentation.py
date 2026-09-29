from PIL import Image

from surgiserve.segmentation import FakeModel


def test_fake_model_returns_a_binary_mask_the_size_of_the_input() -> None:
    image = Image.new("RGB", (40, 30), (20, 20, 20))
    image.paste((240, 240, 240), (20, 0, 40, 30))  # right half bright

    mask = FakeModel().predict(image)

    assert mask.mode == "L"
    assert mask.size == (40, 30)
    assert mask.getpixel((5, 5)) == 0
    assert mask.getpixel((35, 5)) == 255
    assert set(mask.get_flattened_data()) == {0, 255}
