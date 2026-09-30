"""Run the load test across worker counts and PyTorch thread settings.

    python bench/run.py --image PATH [--duration 60] [--warmup 15] [--users 16]

For each configuration it starts the Compose stack from empty volumes with N
workers, waits until /health/ready reports exactly N live workers, drives a
closed-loop Locust load (bench/locustfile.py), lets in-flight jobs finish, and
computes from Postgres timestamps:

- throughput: jobs that succeeded inside the measured window, per second;
- latency, over jobs created inside the window: end to end (created ->
  finished), queue wait (created -> started) and processing (started ->
  finished).

The first ``--warmup`` seconds of each run are excluded (model warm-up,
filling the queue). The first configuration is run again at the end to
detect drift: a fanless laptop slows down as it heats up.

On macOS the Docker VM's memory is ordinary host memory, so if the Mac is
swapping, the VM is too, even when the VM reports free memory. The runner
therefore refuses to start while the host is swapping, records how much the
host swapped out during each run, and flags any run where it did: those
numbers measure the disk, not the service.

Writes bench/results/<timestamp>/results.json and a Markdown table.
"""

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://localhost:8000"
DATABASE_URL = "postgresql+psycopg://surgiserve:surgiserve@localhost:5433/surgiserve"

# (workers, torch threads per worker). 8 CPUs: default (8 each), an even split
# of the CPUs, and one thread each.
CONFIGURATIONS = [(1, 8), (1, 1), (2, 8), (2, 4), (2, 1), (4, 8), (4, 2), (4, 1)]


def sh(*command: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        command, cwd=ROOT, env=env, check=True, capture_output=True, text=True
    ).stdout


# More than this swapped out by the host during a run marks the run invalid.
SWAP_TOLERANCE_MB = 50.0


def host_swapped_out_mb() -> float:
    """Megabytes the Mac has swapped out since boot (from vm_stat)."""

    output = sh("vm_stat")
    page_size = int(output.split("page size of ")[1].split(" bytes")[0])
    for line in output.splitlines():
        if line.startswith("Swapouts:"):
            return int(line.split()[-1].rstrip(".")) * page_size / 2**20
    raise RuntimeError("vm_stat reported no Swapouts line")


def swap_rate_mb(seconds: float = 10.0) -> float:
    before = host_swapped_out_mb()
    time.sleep(seconds)
    return host_swapped_out_mb() - before


def ready() -> dict[str, Any]:
    with urllib.request.urlopen(f"{BASE_URL}/health/ready", timeout=5) as response:
        body: dict[str, Any] = json.load(response)
    return body


def wait_for_workers(count: int, timeout: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            body = ready()
            if body["status"] == "ok" and body["workers"]["alive"] == count:
                return body
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError(f"never saw {count} live workers")


def percentiles(values: list[float]) -> dict[str, float]:
    if len(values) < 2:
        return {}
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return {
        "p50": round(cuts[49], 1),
        "p95": round(cuts[94], 1),
        "p99": round(cuts[98], 1),
        "mean": round(statistics.fmean(values), 1),
    }


def container_usage() -> dict[str, Any]:
    """One `docker stats` sample: CPU % (100 = one CPU) and memory per service."""

    rows = sh(
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
    ).splitlines()
    usage: dict[str, Any] = {
        "worker_cpu_percent": 0.0,
        "workers_mem": [],
        "api_cpu_percent": 0.0,
    }
    for row in rows:
        name, cpu, memory = row.split("\t")
        percent = float(cpu.rstrip("%"))
        if "-worker-" in name:
            usage["worker_cpu_percent"] += percent
            usage["workers_mem"].append(memory.split(" / ")[0])
        elif name.endswith("-api-1"):
            usage["api_cpu_percent"] = percent
    usage["worker_cpu_percent"] = round(usage["worker_cpu_percent"], 1)
    return usage


def measure(
    workers: int, threads: int, args: argparse.Namespace, out: Path
) -> dict[str, Any]:
    label = f"{workers}w-{threads}t"
    print(
        f"== {label}: {workers} worker(s), {threads} torch thread(s) each", flush=True
    )
    env = {**os.environ, "SURGISERVE_TORCH_THREADS": str(threads)}
    sh("docker", "compose", "down", "--volumes", "--timeout", "5", env=env)
    sh(
        "docker",
        "compose",
        "up",
        "--detach",
        "--wait",
        "--scale",
        f"worker={workers}",
        env=env,
    )
    wait_for_workers(workers)

    engine = create_engine(DATABASE_URL)
    with engine.connect() as connection:
        started = connection.scalar(text("SELECT now()"))
    swapped_before = host_swapped_out_mb()
    locust = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "locust",
            "-f",
            "bench/locustfile.py",
            "--headless",
            "--host",
            BASE_URL,
            "-u",
            str(args.users),
            "-r",
            str(args.users),
            "-t",
            f"{args.warmup + args.duration}s",
            "--csv",
            str(out / label),
            "--only-summary",
        ],
        cwd=ROOT,
        env={**os.environ, "BENCH_IMAGE": str(args.image)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(args.warmup + args.duration / 2)
    usage = container_usage()
    locust.wait()
    host_swapped = round(host_swapped_out_mb() - swapped_before, 1)

    # Let jobs still in flight finish, so latency covers every job created in
    # the window, including the slowest.
    with engine.connect() as connection:
        window = {
            "start": connection.scalar(
                text("SELECT CAST(:s AS timestamptz) + make_interval(secs => :w)"),
                {"s": started, "w": args.warmup},
            ),
        }
        window["end"] = connection.scalar(
            text("SELECT CAST(:s AS timestamptz) + make_interval(secs => :d)"),
            {"s": window["start"], "d": args.duration},
        )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            pending = connection.scalar(
                text("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")
            )
        if not pending:
            break
        time.sleep(1)

    with engine.connect() as connection:
        finished_in_window = connection.scalar(
            text(
                "SELECT count(*) FROM jobs WHERE status = 'succeeded' "
                "AND finished_at >= :start AND finished_at < :end"
            ),
            window,
        )
        rows = connection.execute(
            text(
                "SELECT status, "
                "EXTRACT(EPOCH FROM finished_at - created_at) * 1000 AS e2e, "
                "EXTRACT(EPOCH FROM started_at - created_at) * 1000 AS wait, "
                "EXTRACT(EPOCH FROM finished_at - started_at) * 1000 AS proc "
                "FROM jobs WHERE created_at >= :start AND created_at < :end"
            ),
            window,
        ).all()
    engine.dispose()

    succeeded = [row for row in rows if row.status == "succeeded"]
    api = {}
    with (out / f"{label}_stats.csv").open() as stats:
        header = stats.readline().strip().split(",")
        for line in stats:
            fields = dict(zip(header, line.strip().split(","), strict=False))
            if fields["Name"] in ("POST /jobs", "GET /jobs/{id}"):
                api[fields["Name"]] = {
                    "requests": int(fields["Request Count"]),
                    "failures": int(fields["Failure Count"]),
                    "p50_ms": float(fields["50%"]),
                    "p95_ms": float(fields["95%"]),
                }
    result = {
        "label": label,
        "workers": workers,
        "torch_threads": threads,
        "throughput_jobs_per_s": round(finished_in_window / args.duration, 2),
        "jobs_in_window": len(rows),
        "not_succeeded": len(rows) - len(succeeded),
        "end_to_end_ms": percentiles([float(row.e2e) for row in succeeded]),
        "queue_wait_ms": percentiles([float(row.wait) for row in succeeded]),
        "processing_ms": percentiles([float(row.proc) for row in succeeded]),
        "api": api,
        "usage_midrun": usage,
        "host_swapped_out_mb": host_swapped,
        "valid": host_swapped <= SWAP_TOLERANCE_MB,
    }
    print(json.dumps(result), flush=True)
    return result


def environment(image: Path) -> dict[str, Any]:
    info = json.loads(sh("docker", "info", "--format", "{{json .}}"))
    with Image.open(image) as opened:
        size = opened.size
    return {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": sh("git", "rev-parse", "--short", "HEAD").strip(),
        "machine": sh("sysctl", "-n", "hw.model").strip(),
        "chip": sh("sysctl", "-n", "machdep.cpu.brand_string").strip(),
        "cpu_cores": {
            "performance": int(sh("sysctl", "-n", "hw.perflevel0.physicalcpu")),
            "efficiency": int(sh("sysctl", "-n", "hw.perflevel1.physicalcpu")),
        },
        "memory_gb": int(sh("sysctl", "-n", "hw.memsize")) / 2**30,
        "macos": platform.mac_ver()[0],
        "docker_vm": {
            "cpus": info["NCPU"],
            "memory_gb": round(info["MemTotal"] / 2**30, 1),
        },
        "image": {
            "name": image.name,
            "bytes": image.stat().st_size,
            "size": size,
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        },
    }


def table(results: list[dict[str, Any]]) -> str:
    lines = [
        "| Run | Workers | Threads each | Jobs/s | End to end p50 / p95 (ms) "
        "| Processing p50 / p95 (ms) | POST /jobs p95 (ms) | Worker CPU % "
        "| Host swap-out (MB) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        e2e, proc = result["end_to_end_ms"], result["processing_ms"]
        lines.append(
            f"| {result['label']} | {result['workers']} | {result['torch_threads']} "
            f"| {result['throughput_jobs_per_s']} "
            f"| {e2e.get('p50')} / {e2e.get('p95')} "
            f"| {proc.get('p50')} / {proc.get('p95')} "
            f"| {result['api'].get('POST /jobs', {}).get('p95_ms')} "
            f"| {result['usage_midrun']['worker_cpu_percent']} "
            f"| {result['host_swapped_out_mb']}"
            f"{'' if result['valid'] else ' (INVALID: host swapping)'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--warmup", type=int, default=15)
    parser.add_argument("--users", type=int, default=16)
    parser.add_argument(
        "--only", help="comma-separated labels such as 1w-8t,4w-2t (default: all)"
    )
    parser.add_argument(
        "--allow-swapping",
        action="store_true",
        help="run even if the host is swapping (results will be flagged)",
    )
    args = parser.parse_args()
    args.image = args.image.resolve()

    rate = swap_rate_mb()
    print(f"host swapped out {rate:.0f} MB in the last 10 s", flush=True)
    if rate > SWAP_TOLERANCE_MB and not args.allow_swapping:
        sys.exit(
            "the host is swapping, so timings would measure the disk. Free memory "
            "(quit other apps) and try again, or pass --allow-swapping."
        )

    configurations = CONFIGURATIONS
    if args.only:
        wanted = set(args.only.split(","))
        configurations = [c for c in CONFIGURATIONS if f"{c[0]}w-{c[1]}t" in wanted]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "bench" / "results" / stamp
    out.mkdir(parents=True)

    print("building images", flush=True)
    sh("docker", "compose", "build")
    runs = [*configurations, configurations[0]]  # repeat the first: drift check
    results = []
    for index, (workers, threads) in enumerate(runs):
        result = measure(workers, threads, args, out)
        if index == len(runs) - 1 and len(runs) > 1:
            result["label"] += " (repeat)"
        results.append(result)

    summary = {
        "environment": environment(args.image),
        "settings": {
            "users": args.users,
            "warmup_s": args.warmup,
            "duration_s": args.duration,
            "poll_s": float(os.environ.get("BENCH_POLL_SECONDS", "0.1")),
        },
        "results": results,
    }
    (out / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "results.md").write_text(table(results))
    print(table(results))
    print(f"written to {out}")


if __name__ == "__main__":
    main()
