"""The real inference path, checked against surgiseg-ml itself.

The golden tests run everywhere, on a randomly initialised checkpoint: they
compare this service's preprocessing and TTA with surgiseg-ml's own
``_predict_probabilities``, which produced the benchmark. The weights do not
matter for that; the arithmetic around them does.

The real-weights test needs the frozen checkpoint and the Kvasir-Instrument
test images, so it runs only where they exist (not in CI) and skips
elsewhere.
"""

import json
import os
import random
from pathlib import Path

import numpy as np
import pytest
import torch
from PIL import Image
from surgiseg.config import TrainConfig
from surgiseg.data import PairRecord
from surgiseg.evaluation import _predict_probabilities
from surgiseg.model import ResNet18UNet

from surgiserve import random_checkpoint
from surgiserve.settings import FROZEN_THRESHOLD
from surgiserve.surgiseg_model import ChecksumMismatch, SurgisegModel

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_RUN_DIR = Path(
    os.environ.get(
        "SURGISERVE_REFERENCE_RUN_DIR",
        ROOT.parent / "surgiseg-ml/outputs/kvasir-frozen-final-2026-08-03",
    )
)
REFERENCE_IMAGES_DIR = Path(
    os.environ.get(
        "SURGISERVE_REFERENCE_IMAGES_DIR",
        ROOT.parent
        / "surgiseg-ml/data/kvasir-instrument/extracted/kvasir-instrument/images",
    )
)


@pytest.fixture(scope="module")
def weights(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    directory = tmp_path_factory.mktemp("weights")
    return directory, random_checkpoint.write(directory, seed=0)


def load(
    weights: tuple[Path, dict[str, str]], *, flip_tta: bool = True
) -> SurgisegModel:
    directory, hashes = weights
    return SurgisegModel.load(
        directory / "best-model.pt",
        directory / "config.json",
        checkpoint_sha256=hashes["checkpoint_sha256"],
        config_sha256=hashes["config_sha256"],
        threshold=FROZEN_THRESHOLD,
        flip_tta=flip_tta,
    )


@pytest.fixture(scope="module")
def image_records(tmp_path_factory: pytest.TempPathFactory) -> list[PairRecord]:
    """Synthetic images in surgiseg-ml's layout: images/ plus same-size masks/.

    Sizes and aspect ratios differ, so the resize step is really exercised.
    """

    root = tmp_path_factory.mktemp("frames")
    (root / "images").mkdir()
    (root / "masks").mkdir()
    records = []
    for index, size in enumerate([(160, 120), (97, 211), (128, 128)]):
        stem = f"synthetic-{index}"
        pixels = random.Random(index).randbytes(size[0] * size[1] * 3)
        Image.frombytes("RGB", size, pixels).save(root / "images" / f"{stem}.png")
        Image.new("L", size, 0).save(root / "masks" / f"{stem}.png")
        records.append(
            PairRecord(
                stem=stem,
                image_path=root / "images" / f"{stem}.png",
                mask_path=root / "masks" / f"{stem}.png",
            )
        )
    return records


def reference(
    weights: tuple[Path, dict[str, str]], records: list[PairRecord], tta: str
) -> np.ndarray:
    directory, _ = weights
    config = TrainConfig(**json.loads((directory / "config.json").read_text()))
    probabilities: np.ndarray
    probabilities, _targets = _predict_probabilities(
        directory / "best-model.pt",
        records,
        config,
        device=torch.device("cpu"),
        tta=tta,
    )
    return probabilities


def ours(model: SurgisegModel, records: list[PairRecord]) -> np.ndarray:
    outputs = []
    for record in records:
        with Image.open(record.image_path) as image:
            outputs.append(model.probabilities(image).numpy())
    return np.stack(outputs)


def test_probabilities_match_surgiseg_ml_with_flip_tta(
    weights: tuple[Path, dict[str, str]], image_records: list[PairRecord]
) -> None:
    expected = reference(weights, image_records, "horizontal-flip")

    actual = ours(load(weights), image_records)

    assert actual.shape == expected.shape == (3, 128, 128)
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)


def test_probabilities_match_surgiseg_ml_without_tta(
    weights: tuple[Path, dict[str, str]], image_records: list[PairRecord]
) -> None:
    expected = reference(weights, image_records, "none")

    actual = ours(load(weights, flip_tta=False), image_records)

    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)


def test_the_normalisation_trap_is_caught(
    weights: tuple[Path, dict[str, str]], image_records: list[PairRecord]
) -> None:
    # Built by hand with defaults, the network loads the same weights without
    # complaint but skips ImageNet normalisation. The golden comparison is
    # what notices.
    directory, _ = weights
    wrong = ResNet18UNet()  # normalize_for_imagenet=False
    state = torch.load(directory / "best-model.pt", weights_only=True)
    wrong.load_state_dict(state["model_state"])
    model = SurgisegModel(
        wrong,
        TrainConfig(**random_checkpoint.FROZEN_CONFIG),
        threshold=FROZEN_THRESHOLD,
        flip_tta=True,
        name="wrong",
    )

    difference = np.abs(
        ours(model, image_records)
        - reference(weights, image_records, "horizontal-flip")
    )

    assert difference.max() > 1e-3


def test_threshold_is_the_benchmarks_not_the_training_configs(
    weights: tuple[Path, dict[str, str]],
) -> None:
    directory, _ = weights
    assert json.loads((directory / "config.json").read_text())["threshold"] == 0.5

    assert load(weights).threshold == 0.47


@pytest.mark.parametrize(("probability", "expected"), [(0.47, 255), (0.4699, 0)])
def test_mask_threshold_is_inclusive_and_resized_to_the_input(
    weights: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    probability: float,
    expected: int,
) -> None:
    model = load(weights)
    monkeypatch.setattr(
        model, "probabilities", lambda _image: torch.full((128, 128), probability)
    )

    mask = model.predict(Image.new("RGB", (300, 200)))

    assert (mask.mode, mask.size) == ("L", (300, 200))
    assert set(mask.get_flattened_data()) == {expected}


def test_a_wrong_checkpoint_hash_refuses_to_load(
    weights: tuple[Path, dict[str, str]],
) -> None:
    directory, hashes = weights

    with pytest.raises(ChecksumMismatch, match="best-model.pt"):
        SurgisegModel.load(
            directory / "best-model.pt",
            directory / "config.json",
            checkpoint_sha256="0" * 64,
            config_sha256=hashes["config_sha256"],
            threshold=FROZEN_THRESHOLD,
            flip_tta=True,
        )


def test_a_wrong_config_hash_refuses_to_load(
    weights: tuple[Path, dict[str, str]],
) -> None:
    directory, hashes = weights

    with pytest.raises(ChecksumMismatch, match="config.json"):
        SurgisegModel.load(
            directory / "best-model.pt",
            directory / "config.json",
            checkpoint_sha256=hashes["checkpoint_sha256"],
            config_sha256="0" * 64,
            threshold=FROZEN_THRESHOLD,
            flip_tta=True,
        )


def test_the_real_checkpoint_reproduces_the_frozen_benchmark() -> None:
    """With the real weights, this service's probabilities on real test images
    must equal the ones the frozen benchmark scored (Dice 0.9226)."""

    checkpoint = REFERENCE_RUN_DIR / "best-model.pt"
    cache = REFERENCE_RUN_DIR / "probability-cache" / "test-horizontal-flip.npz"
    if not (checkpoint.exists() and cache.exists() and REFERENCE_IMAGES_DIR.is_dir()):
        pytest.skip("needs the frozen surgiseg-ml checkpoint and Kvasir-Instrument")

    from surgiserve.settings import FROZEN_CHECKPOINT_SHA256, FROZEN_CONFIG_SHA256

    model = SurgisegModel.load(
        checkpoint,
        REFERENCE_RUN_DIR / "config.json",
        checkpoint_sha256=FROZEN_CHECKPOINT_SHA256,
        config_sha256=FROZEN_CONFIG_SHA256,
        threshold=FROZEN_THRESHOLD,
        flip_tta=True,
    )
    saved = np.load(cache)
    for index in range(0, len(saved["stems"]), 20):  # 6 of the 118 images
        stem = str(saved["stems"][index])
        with Image.open(REFERENCE_IMAGES_DIR / f"{stem}.jpg") as image:
            actual = model.probabilities(image).numpy()
        expected = saved["probabilities"][index]
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-5, err_msg=stem)
        assert np.array_equal(actual >= 0.47, expected >= 0.47), stem


def test_the_thread_limit_reaches_openmp_in_a_fresh_process(
    weights: tuple[Path, dict[str, str]],
) -> None:
    # A new interpreter, as in the worker container: the limit has to be in
    # place before torch is first imported for OpenMP (and oneDNN) to use it.
    import subprocess
    import sys

    directory, hashes = weights
    script = (
        "from pathlib import Path\n"
        "from surgiserve.settings import Settings\n"
        "from surgiserve.worker import build_model\n"
        "import os, sys\n"
        "assert 'torch' not in sys.modules\n"
        f"build_model(Settings(model='surgiseg', torch_threads=1,"
        f" model_checkpoint_path=Path({str(directory / 'best-model.pt')!r}),"
        f" model_config_path=Path({str(directory / 'config.json')!r}),"
        f" model_checkpoint_sha256={hashes['checkpoint_sha256']!r},"
        f" model_config_sha256={hashes['config_sha256']!r}))\n"
        "import torch\n"
        "print(os.environ['OMP_NUM_THREADS'], torch.get_num_threads())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        # The working tree, as pytest itself uses (pyproject's pythonpath).
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )

    assert result.stdout.split() == ["1", "1"]


def test_the_worker_applies_its_torch_thread_setting(
    weights: tuple[Path, dict[str, str]],
) -> None:
    from surgiserve.settings import Settings
    from surgiserve.worker import build_model

    directory, hashes = weights
    settings = Settings(
        model="surgiseg",
        model_checkpoint_path=directory / "best-model.pt",
        model_config_path=directory / "config.json",
        model_checkpoint_sha256=hashes["checkpoint_sha256"],
        model_config_sha256=hashes["config_sha256"],
        torch_threads=2,
    )
    before = torch.get_num_threads()
    try:
        build_model(settings)
        assert torch.get_num_threads() == 2
    finally:
        torch.set_num_threads(before)
