#!/usr/bin/env bash
# Invoked via SSH after CI has checked this exact main commit.
set -euo pipefail
sha=${1:?Supply the tested 40-character main commit SHA}
live=${2:-true}
[[ "$sha" =~ ^[a-f0-9]{40}$ ]] || { echo 'Invalid commit'; exit 1; }
[[ "$live" == true || "$live" == false ]] || exit 1
cd "${DEPLOY_DIRECTORY:-/home/the-neural-network}"
exec 9>.deployment.lock
flock -n 9 || { echo 'Another deployment is running'; exit 1; }
git_cmd=(git -c "safe.directory=$PWD")
[[ -z "$("${git_cmd[@]}" status --porcelain --untracked-files=no)" ]] || {
  echo 'Commit or resolve tracked VM changes before deployment'; exit 1;
}
[[ -f .env && -s artifacts/model.joblib ]] || { echo 'Missing VM .env or trained model'; exit 1; }
exec 8>artifacts/.daily.lock
flock -n 8 || { echo 'Daily model training or promotion is running; retry later'; exit 1; }
"${git_cmd[@]}" fetch origin main
[[ "$("${git_cmd[@]}" rev-parse origin/main)" == "$sha" ]] || {
  echo 'main has changed; rerun the workflow on latest main'; exit 1;
}
previous=$("${git_cmd[@]}" rev-parse HEAD)
echo "Previous checkout: $previous; deploying: $sha"
"${git_cmd[@]}" checkout --detach "$sha"
export DEPLOY_TAG="$sha"
export DAILY_MODEL_IMAGE="the-neural-network:$sha"
export LOCAL_UID=$(id -u) LOCAL_GID=$(id -g)
compose=(docker compose -p the-neural-network)
if [[ "$live" == true ]]; then compose+=(--profile live); fi
"${compose[@]}" config --quiet
"${compose[@]}" build api
if [[ "$live" == true ]]; then
  "${compose[@]}" run --rm --no-deps cold-start-worker python -c '
import os
required = ("USER_LOOKUP_URL_TEMPLATE", "OPENAI_API_KEY")
missing = [key for key in required if not os.getenv(key, "").strip()]
assert not missing, "Missing live configuration: " + ", ".join(missing)'
  "${compose[@]}" run --rm --no-deps kafka-tunnel \
    bash /app/deployment/kafka_tunnel.sh --check
fi
"${compose[@]}" up -d --wait --wait-timeout 120
"${compose[@]}" exec -T api python -c '
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:8082/recommend/1", timeout=0.6) as r:
    body = r.read().decode()
    ids = body.split(",")
    assert r.status == 200 and 1 <= len(ids) <= 20
    assert all(ids) and len(set(ids)) == len(ids) and "\n" not in body
print("Deployment recommendation check passed")'
mkdir -p state
printf '%s\n' "$sha" > state/deployed-commit.txt
printf '%s\n' "$previous" > state/previous-commit.txt
echo "Deployed $sha successfully. Existing model, .env and database volumes retained."
