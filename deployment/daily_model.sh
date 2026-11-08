#!/usr/bin/env bash
# Run from a trusted VM checkout. Shares locks with code deployments and timer.
set -euo pipefail
cd "$(dirname "$0")/.."
action=${1:-run}
day=${2:-$(TZ=America/New_York date -d yesterday +%F)}
[[ "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || exit 1
[[ "$action" == run || "$action" == promote || "$action" == rollback ]] || exit 1
exec 9>.deployment.lock
flock -n 9 || { echo 'Code deployment is running'; exit 1; }
exec 8>artifacts/.daily.lock
flock -n 8 || { echo 'Daily model job is running'; exit 1; }
export LOCAL_UID=$(id -u) LOCAL_GID=$(id -g)
# Use the exact image already running in production, including its tag.
api_id=$(docker compose -p the-neural-network ps -q api)
[[ -n "$api_id" ]] || { echo 'API must be running'; exit 1; }
export DAILY_MODEL_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$api_id")
compose=(docker compose -p the-neural-network)
tool() { "${compose[@]}" run --rm --no-deps --entrypoint python daily-model-updater "$@"; }
if [[ "$action" == run ]]; then
  tool -m src.model_updates.daily --day "$day"
  exit
fi
verify() {
  expected=$(sha256sum artifacts/model.joblib | cut -d ' ' -f 1)
  "${compose[@]}" restart api && "${compose[@]}" exec -T api python -c '
import json, sys, time, urllib.request
expected = sys.argv[1]
for attempt in range(30):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8082/health", timeout=2) as r:
            assert json.load(r)["model_sha256"] == expected
        with urllib.request.urlopen("http://127.0.0.1:8082/recommend/1", timeout=.6) as r:
            body = r.read().decode(); ids = body.split(",")
            assert 1 <= len(ids) <= 20 and all(ids) and len(ids) == len(set(ids)) and "\n" not in body
        sys.exit(0)
    except Exception:
        time.sleep(1)
sys.exit(1)' "$expected"
}
if [[ "$action" == rollback ]]; then
  tool -m src.model_updates.promotion rollback "$day"
  verify
  echo 'Previous artifact restored and API verified.'
  exit
fi
tool -m src.model_updates.promotion stage "$day"
if verify; then
  tool -m src.model_updates.promotion finish "$day"
  echo 'Model promoted and verified.'
else
  tool -m src.model_updates.promotion rollback "$day"
  verify || { echo 'Rollback artifact restored but API verification failed; inspect containers.' >&2; exit 1; }
  echo 'Promotion failed; previous artifact restored and API verified.' >&2
  exit 1
fi
