#!/usr/bin/env bash
# Run from a trusted VM checkout. Shares locks with code deployments and timer.
set -euo pipefail
cd "$(dirname "$0")/.."
action=${1:-run}
day=${2:-$(TZ=America/New_York date -d yesterday +%F)}
[[ "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || exit 1
[[ "$action" == run || "$action" == auto || "$action" == promote || "$action" == rollback || "$action" == register || "$action" == register-champion ]] || exit 1
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
tool() { "${compose[@]}" run --rm --no-deps -T --interactive=false --entrypoint python daily-model-updater "$@" </dev/null; }
if [[ "$action" == run ]]; then
  tool -m src.model_updates.daily --day "$day"
  exit
fi
if [[ "$action" == register ]]; then
  tool -m src.model_updates.promotion register "$day"
  exit
fi
# Older deployment revisions must be upgraded before using this script.
bootstrap_version=$("${compose[@]}" exec -T api python -c '
import json, sys, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8082/health", timeout=2) as response:
    health = json.load(response)
    if not health.get("activation_enabled") or not health.get("registry_ready"):
        sys.exit("Serving API must support live model activation")
    print(health["model_version"])' </dev/null)
if [[ "$action" == register-champion ]]; then
  tool -m src.model_updates.promotion register-champion "$day" --bootstrap-version "$bootstrap_version"
  exit
fi
if [[ "$action" == auto ]]; then
  if ! tool -m src.model_updates.daily --day "$day" --retry-failed; then
    tool -m src.model_updates.automatic decide "$day" || true
    exit 1
  fi
  # A database audit must succeed before production is touched. Exit 10 is a normal skip.
  if tool -m src.model_updates.automatic decide "$day"; then
    echo 'Automatic comparison checks passed; promoting candidate.'
  else
    status=$?
    [[ "$status" == 10 ]] && exit 0
    exit "$status"
  fi
fi
verify() {
  "${compose[@]}" exec -T api python -c '
import json, sys, time, urllib.request
activation_id, expected = sys.argv[1:]
for attempt in range(60):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8082/health", timeout=2) as response:
            health = json.load(response)
    except (OSError, ValueError):
        time.sleep(.5)
        continue
    if health.get("activation_id") == activation_id:
        if health.get("activation_status") == "failed":
            sys.exit("Model activation failed: " + str(health.get("activation_error")))
        if health.get("activation_status") == "loaded" and health["model_sha256"] == expected:
            break
    time.sleep(.5)
else:
    sys.exit("Model activation acknowledgement timed out")
with urllib.request.urlopen("http://127.0.0.1:8082/recommend/1", timeout=.6) as response:
    body = response.read().decode()
    ids = body.split(",")
    assert 1 <= len(ids) <= 20 and all(ids) and len(set(ids)) == len(ids) and "\n" not in body
' "$1" "$2" </dev/null
}
rollback() {
  local result activation_id expected
  result=$(tool -m src.model_updates.promotion rollback "$day") || return 1
  read -r activation_id expected <<< "$result"
  verify "$activation_id" "$expected" || return 1
  tool -m src.model_updates.promotion finish-rollback "$day" "$activation_id" "$expected" || return 1
}
if [[ "$action" == rollback ]]; then
  rollback
  echo 'Previous model restored and API verified.'
  exit
fi
result=$(tool -m src.model_updates.promotion stage "$day" --bootstrap-version "$bootstrap_version")
read -r activation_id expected <<< "$result"
if verify "$activation_id" "$expected"; then
  tool -m src.model_updates.promotion finish "$day" "$activation_id" "$expected"
  if [[ "$action" == auto ]]; then tool -m src.model_updates.automatic record "$day"; fi
  echo 'Model activated and verified without restarting the API.'
else
  rollback || { echo 'Rollback verification failed; promotion remains pending.' >&2; exit 1; }
  if [[ "$action" == auto ]]; then tool -m src.model_updates.automatic record "$day"; fi
  echo 'Promotion failed; previous model restored and API verified.' >&2
  exit 1
fi
