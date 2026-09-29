"""SurgiServe: an educational inference service for surgiseg-ml."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("surgiserve")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+unknown"
