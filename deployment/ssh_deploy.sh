#!/usr/bin/env bash
set -euo pipefail
: "${DEPLOY_HOST:?Set DEPLOY_HOST}"
: "${DEPLOY_USER:?Set DEPLOY_USER}"
: "${DEPLOY_SSH_KEY:?Set DEPLOY_SSH_KEY}"
: "${DEPLOY_KNOWN_HOSTS:?Set verified DEPLOY_KNOWN_HOSTS}"
[[ "$DEPLOY_HOST" =~ ^[a-zA-Z0-9.-]+$ && "$DEPLOY_USER" =~ ^[a-zA-Z0-9_-]+$ ]]
[[ "${TARGET_SHA:-}" =~ ^[a-f0-9]{40}$ ]]
[[ "${ENABLE_LIVE:-}" == true || "${ENABLE_LIVE:-}" == false ]]
umask 077
key_dir=$(mktemp -d)
trap 'rm -f "$key_dir/key" "$key_dir/known_hosts"; rmdir "$key_dir"' EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$key_dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$key_dir/known_hosts"
ssh -i "$key_dir/key" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$key_dir/known_hosts" \
  -o ConnectTimeout=15 "$DEPLOY_USER@$DEPLOY_HOST" \
  "bash -s -- $TARGET_SHA $ENABLE_LIVE" < deployment/deploy_vm.sh
