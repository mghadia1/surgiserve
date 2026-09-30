#!/usr/bin/env bash
# End-to-end check against a running Compose stack, over real HTTP:
# health and readiness, upload an image, wait for the worker to finish the job, download the
# mask and check it is a PNG the size of the upload, and confirm both files
# are on the storage volume inside the container.
set -euo pipefail

base_url="${BASE_URL:-http://localhost:8000}"
image="${1:-tests/fixtures/checkerboard-256.png}"
timeout_seconds="${SMOKE_TIMEOUT_SECONDS:-30}"

echo "GET /health"
curl --fail --silent --show-error "$base_url/health"
echo

echo "GET /health/ready"
ready="$(curl --silent --show-error "$base_url/health/ready")"
python3 - "$ready" <<'PY'
import json, sys
ready = json.loads(sys.argv[1])
failing = [name for name, check in ready["checks"].items() if not check["ok"]]
assert ready["status"] == "ok", f"not ready: {failing} {ready['checks']}"
assert ready["workers"]["alive"] >= 1, "no live worker"
print(f"ready: all checks pass; workers {ready['workers']['models']}")
PY

echo "POST /jobs ($image)"
headers="$(mktemp)"
trap 'rm -f "$headers"' EXIT
created="$(curl --fail --silent --show-error --dump-header "$headers" \
  --form "image=@$image" "$base_url/jobs")"
echo "$created"
location="$(grep -i '^location:' "$headers" | tr -d '\r' | awk '{print $2}')"

status() { python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'; }

echo "GET $location until the worker finishes (up to ${timeout_seconds}s)"
deadline=$(( $(date +%s) + timeout_seconds ))
while :; do
  fetched="$(curl --fail --silent --show-error "$base_url$location")"
  current="$(status <<<"$fetched")"
  case "$current" in
    succeeded) break ;;
    failed) echo "job failed: $fetched" >&2; exit 1 ;;
  esac
  if (( $(date +%s) >= deadline )); then
    echo "job still $current after ${timeout_seconds}s: $fetched" >&2
    exit 1
  fi
  sleep 0.5
done
echo "$fetched"

paths="$(python3 - "$created" "$fetched" <<'PY'
import json, sys
created, fetched = (json.loads(arg) for arg in sys.argv[1:3])
assert fetched["input"] == created["input"], "the stored input changed"
assert fetched["started_at"] and fetched["finished_at"], fetched
extension = {"PNG": "png", "JPEG": "jpg"}[fetched["input"]["format"]]
print(f"/data/storage/inputs/{fetched['id']}.{extension}")
print(f"/data/storage/masks/{fetched['id']}.png")
PY
)"

mask_url="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["mask_url"])' <<<"$fetched")"
echo "GET $mask_url"
mask="$(mktemp)"
trap 'rm -f "$headers" "$mask"' EXIT
curl --fail --silent --show-error --output "$mask" "$base_url$mask_url"
python3 - "$mask" "$fetched" <<'PY'
import json, struct, sys
data = open(sys.argv[1], "rb").read()
job = json.loads(sys.argv[2])
assert data[:8] == b"\x89PNG\r\n\x1a\n", "the mask is not a PNG"
width, height = struct.unpack(">II", data[16:24])  # from the IHDR chunk
expected = (job["input"]["width"], job["input"]["height"])
assert (width, height) == expected, f"mask is {width}x{height}, upload {expected}"
print(f"mask: PNG {width}x{height}, {len(data)} bytes, model {job['model']}")
PY

for path in $paths; do
  echo "on the volume: $path"
  docker compose exec -T api test -s "$path"
done
echo "smoke test passed"
