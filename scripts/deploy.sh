#!/usr/bin/env bash
# Runs on the dedicated runner; ships only the tested Git revision.
set -euo pipefail
umask 077
qqbot_target="${DEPLOY_PATH:-/home/ubuntu/qqBots2.0}"
qqbot_mode="${DEPLOY_TRANSPORT:-${CHATBOT_DEPLOY_TRANSPORT:-local}}"
[[ "$qqbot_target" =~ ^/[a-zA-Z0-9_./-]+$ && "$qqbot_target" != *..* && "$qqbot_target" != / && "$qqbot_target" != */ ]]
qqbot_revision="$(git rev-parse HEAD)"
[[ "${GITHUB_SHA:-$qqbot_revision}" == "$qqbot_revision" ]]
qqbot_tmp="$(mktemp -d)"
trap 'rm -rf -- "$qqbot_tmp"' EXIT
git archive --format=tar.gz HEAD > "$qqbot_tmp/release.tar.gz"
if [[ "$qqbot_mode" == local ]]; then
  [[ "$(id -u)" != 0 && -f "$qqbot_target/data/cmd_config.json" ]]
  mkdir -p "$qqbot_target/runtime/deploy"
  cp "$qqbot_tmp/release.tar.gz" "$qqbot_target/runtime/deploy/$qqbot_revision.tar.gz"
  bash scripts/deploy-remote.sh "$qqbot_target" "$qqbot_revision"
elif [[ "$qqbot_mode" == ssh ]]; then
  qqbot_host="${DEPLOY_HOST:-njuse}"
  [[ "$qqbot_host" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]
  qqbot_ssh=(ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=20
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
  if [[ -n "${DEPLOY_SSH_KEY:-}" ]]; then
    : "${DEPLOY_KNOWN_HOSTS:?missing known_hosts}"
    printf '%s\n' "$DEPLOY_SSH_KEY" > "$qqbot_tmp/key"
    printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$qqbot_tmp/known_hosts"
    qqbot_ssh+=(-i "$qqbot_tmp/key" -o IdentitiesOnly=yes -o "UserKnownHostsFile=$qqbot_tmp/known_hosts")
    unset DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS
  fi
  if [[ -n "${DEPLOY_USER:-}" ]]; then
    [[ "$DEPLOY_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]]
    qqbot_ssh+=(-l "$DEPLOY_USER")
  fi
  if [[ -n "${DEPLOY_PORT:-}" ]]; then
    [[ "$DEPLOY_PORT" =~ ^[0-9]+$ && "$DEPLOY_PORT" -ge 1 && "$DEPLOY_PORT" -le 65535 ]]
    qqbot_ssh+=(-p "$DEPLOY_PORT")
  fi
  qqbot_ssh+=("$qqbot_host")
  "${qqbot_ssh[@]}" "test -f '$qqbot_target/data/cmd_config.json' && umask 077 && mkdir -p '$qqbot_target/runtime/deploy'"
  "${qqbot_ssh[@]}" "umask 077; cat > '$qqbot_target/runtime/deploy/$qqbot_revision.tar.gz'" < "$qqbot_tmp/release.tar.gz"
  "${qqbot_ssh[@]}" bash -s -- "$qqbot_target" "$qqbot_revision" < scripts/deploy-remote.sh
else
  echo 'DEPLOY_TRANSPORT must be local or ssh' >&2
  exit 1
fi
