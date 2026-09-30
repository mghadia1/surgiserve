"""Run the API: ``python -m surgiserve.serve``.

A small entry point instead of the ``uvicorn`` command, so logging is set up
before uvicorn starts and uvicorn's own messages come out as JSON too
(``log_config=None`` stops it installing its own handlers). Uvicorn's access
log is off because ``logs.RequestLogging`` writes one line per request with
more in it.
"""

import os

import uvicorn

from .logs import configure_logging
from .settings import Settings


def main() -> None:
    settings = Settings()
    configure_logging("api", log_format=settings.log_format, level=settings.log_level)
    uvicorn.run(
        "surgiserve.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
