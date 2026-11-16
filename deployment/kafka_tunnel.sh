#!/usr/bin/env bash
set -euo pipefail

required_variables=(
  KAFKA_SSH_HOST
  KAFKA_SSH_USER
  KAFKA_SSH_PORT
  KAFKA_REMOTE_HOST
  KAFKA_REMOTE_PORT
  KAFKA_TUNNEL_LOCAL_PORT
  KAFKA_TOPIC
  KAFKA_GROUP_ID
  KAFKA_SSH_KNOWN_HOSTS
  SSHPASS
)
for variable_name in "${required_variables[@]}"; do
  [[ -n "${!variable_name:-}" ]] || {
    echo "Missing tunnel configuration: $variable_name" >&2
    exit 1
  }
done

known_hosts_file=${KAFKA_SSH_KNOWN_HOSTS_FILE:-/tmp/kafka_known_hosts}
printf '%s\n' "$KAFKA_SSH_KNOWN_HOSTS" > "$known_hosts_file"
chmod 600 "$known_hosts_file"

[[ "${1:-}" == "--check" ]] && exit 0

exec sshpass -e ssh -NT \
  -p "$KAFKA_SSH_PORT" \
  -o BatchMode=no \
  -o PreferredAuthentications=password,keyboard-interactive \
  -o PubkeyAuthentication=no \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$known_hosts_file" \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L "127.0.0.1:${KAFKA_TUNNEL_LOCAL_PORT}:${KAFKA_REMOTE_HOST}:${KAFKA_REMOTE_PORT}" \
  "${KAFKA_SSH_USER}@${KAFKA_SSH_HOST}"
