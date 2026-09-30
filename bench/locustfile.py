"""Load for SurgiServe: a closed loop.

Each simulated user uploads an image, polls its job until it finishes, and
then uploads the next. With more users than worker capacity, the queue never
empties, so throughput measures what the workers can do.

Locust's own numbers here are HTTP latencies (how fast the API answers).
Job throughput and latency are computed afterwards from the timestamps
Postgres recorded (bench/run.py), which polling granularity does not blur.
"""

import os
import time
from pathlib import Path

from locust import HttpUser, constant, task

IMAGE = Path(os.environ["BENCH_IMAGE"])
POLL_SECONDS = float(os.environ.get("BENCH_POLL_SECONDS", "0.1"))
JOB_TIMEOUT_SECONDS = 120.0
FINAL = {"succeeded", "failed", "dead_lettered"}
CONTENT_TYPE = "image/png" if IMAGE.suffix.lower() == ".png" else "image/jpeg"


class Submitter(HttpUser):
    wait_time = constant(0)

    def on_start(self) -> None:
        self.image = IMAGE.read_bytes()

    @task
    def submit_and_wait(self) -> None:
        with self.client.post(
            "/jobs",
            files={"image": (IMAGE.name, self.image, CONTENT_TYPE)},
            name="POST /jobs",
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"status {response.status_code}")
                return
            job_id = response.json()["id"]

        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)  # gevent-patched: yields to other users
            polled = self.client.get(f"/jobs/{job_id}", name="GET /jobs/{id}")
            if polled.ok and polled.json()["status"] in FINAL:
                return
