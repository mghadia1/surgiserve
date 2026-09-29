import os
from pathlib import Path

import pytest

from surgiserve import storage


def test_writes_the_exact_bytes_and_creates_parents(tmp_path: Path) -> None:
    written = storage.save_atomically(tmp_path, "inputs/a.png", b"\x89PNG data")

    assert written == tmp_path / "inputs" / "a.png"
    assert written.read_bytes() == b"\x89PNG data"
    assert list(written.parent.iterdir()) == [written]  # no .part file left


def test_a_failed_write_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)

    with pytest.raises(OSError, match="disk full"):
        storage.save_atomically(tmp_path, "inputs/a.png", b"data")

    assert list((tmp_path / "inputs").iterdir()) == []
