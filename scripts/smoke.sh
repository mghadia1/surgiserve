#!/usr/bin/env bash
# End-to-end check against a running Compose stack, over real HTTP:
# health, upload an image, fetch the job back, confirm the file is on the
# storage volume inside the container.
set -euo pipefail

base_url="${BASE_URL:-http://localhost:8000}"
image="${1:-tests/fixtures/checkerboard-256.png}"

echo "GET /health"
curl --fail --silent --show-error "$base_url/health"
echo

echo "POST /jobs ($image)"
headers="$(mktemp)"
trap 'rm -f "$headers"' EXIT
created="$(curl --fail --silent --show-error --dump-header "$headers" \
  --form "image=@$image" "$base_url/jobs")"
echo "$created"
location="$(grep -i '^location:' "$headers" | tr -d '\r' | awk '{print $2}')"

echo "GET $location"
fetched="$(curl --fail --silent --show-error "$base_url$location")"
echo "$fetched"

stored_path="$(python3 - "$created" "$fetched" <<'PY'
import json, sys
created, fetched = (json.loads(arg) for arg in sys.argv[1:3])
assert fetched == created, "GET returned something other than what POST did"
assert fetched["status"] == "queued", fetched["status"]
extension = {"PNG": "png", "JPEG": "jpg"}[fetched["input"]["format"]]
print(f"/data/storage/inputs/{fetched['id']}.{extension}")
PY
)"

echo "stored file: $stored_path"
docker compose exec -T api test -s "$stored_path"
echo "smoke test passed"
