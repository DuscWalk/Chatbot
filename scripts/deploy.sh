#!/usr/bin/env bash
# Runs on the GitHub Actions runner. Only tracked code is uploaded.
set -euo pipefail
umask 077
for qqbot_variable in DEPLOY_HOST DEPLOY_USER DEPLOY_PATH DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS; do
  if [[ -z "${!qqbot_variable:-}" ]]; then
    echo "缺少 GitHub production 配置: $qqbot_variable" >&2
    exit 1
  fi
done
qqbot_port="${DEPLOY_PORT:-22}"
# These values also appear in remote command strings: allow only literal paths/names.
[[ "$DEPLOY_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo 'DEPLOY_HOST 必须是主机名或 IPv4 地址' >&2; exit 1; }
[[ "$DEPLOY_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || { echo 'DEPLOY_USER 格式错误' >&2; exit 1; }
[[ "$DEPLOY_PATH" =~ ^/[a-zA-Z0-9_./-]+$ && "$DEPLOY_PATH" != *..* && "$DEPLOY_PATH" != */ ]] || { echo 'DEPLOY_PATH 必须是无空格的绝对项目路径，末尾不带 /' >&2; exit 1; }
[[ "$qqbot_port" =~ ^[0-9]+$ && "$qqbot_port" -ge 1 && "$qqbot_port" -le 65535 ]] || { echo 'DEPLOY_PORT 格式错误' >&2; exit 1; }
qqbot_revision="$(git rev-parse HEAD)"
qqbot_tmp="$(mktemp -d)"
trap 'rm -rf -- "$qqbot_tmp"' EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$qqbot_tmp/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$qqbot_tmp/known_hosts"
# Do not pass private key material into SSH's remote environment.
unset DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS
qqbot_ssh=(ssh -i "$qqbot_tmp/key" -p "$qqbot_port"
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$qqbot_tmp/known_hosts" -o ConnectTimeout=20
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3
  "$DEPLOY_USER@$DEPLOY_HOST")
git archive --format=tar.gz HEAD > "$qqbot_tmp/release.tar.gz"
# Preflight before creating any deployment files on the server.
"${qqbot_ssh[@]}" bash -s -- "$DEPLOY_PATH" <<'REMOTE'
set -euo pipefail
qqbot_target="$1"
[[ "$(id -u)" != 0 && "$qqbot_target" != / && "$qqbot_target" != "$HOME" ]]
if [[ -d "$qqbot_target" ]] && [[ ! -f "$qqbot_target/bot" || ! -f "$qqbot_target/compose.yaml" ]]; then
  if [[ -n "$(find "$qqbot_target" -mindepth 1 -maxdepth 1 ! -name runtime -print -quit)" ]]; then
    echo '目标目录已有其他文件，拒绝覆盖。请使用专用项目目录。' >&2
    exit 1
  fi
fi
docker info >/dev/null
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user show-environment >/dev/null
umask 077
mkdir -p -- "$qqbot_target/runtime/deploy"
REMOTE
"${qqbot_ssh[@]}" "umask 077; cat > '$DEPLOY_PATH/runtime/deploy/$qqbot_revision.tar.gz'" < "$qqbot_tmp/release.tar.gz"
"${qqbot_ssh[@]}" bash -s -- "$DEPLOY_PATH" "$qqbot_revision" < scripts/deploy-remote.sh
