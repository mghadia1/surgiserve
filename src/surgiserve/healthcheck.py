"""Container health checks: ``python -m surgiserve.healthcheck api|worker``.

Exits 0 if healthy, 1 if not. The slim image has no curl, so these use the
standard library.

- ``api``: ``GET /health`` answers (liveness only; see ``health.py`` for why
  the container check does not use readiness).
- ``worker``: the worker's main loop has touched its liveness file recently.
  That shows the loop itself is turning, which the Redis heartbeat, sent from
  a separate thread, does not.
"""

import sys
import time
import urllib.request

from .settings import Settings


def api_is_alive() -> bool:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8000/health", timeout=2
        ) as response:
            return bool(response.status == 200)
    except OSError:
        return False


def worker_is_alive(settings: Settings) -> bool:
    try:
        age = time.time() - settings.worker_liveness_file.stat().st_mtime
    except FileNotFoundError:
        return False
    return age <= settings.worker_liveness_max_age_seconds


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "api"
    healthy = api_is_alive() if target == "api" else worker_is_alive(Settings())
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
