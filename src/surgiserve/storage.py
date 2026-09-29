"""File storage on the local volume."""

import os
import tempfile
from pathlib import Path


def save_atomically(storage_dir: Path, relative_path: str, data: bytes) -> Path:
    """Write ``data`` to ``storage_dir / relative_path`` all at once or not at all.

    The bytes go to a temporary file in the same directory, are flushed to
    disk, and are then renamed into place. A rename within one file system is
    atomic, so a reader never sees a half-written image, and a crash leaves at
    most a ``.part`` file, never a truncated image under a real name.
    """

    destination = storage_dir / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=".upload-", suffix=".part"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination
