#!/usr/bin/env bash
# Executed on Ubuntu via SSH, as the ordinary deployment user.
set -euo pipefail
umask 077
qqbot_target="${1:?project directory required}"
qqbot_revision="${2:?revision required}"
[[ "$qqbot_target" =~ ^/[a-zA-Z0-9_./-]+$ && "$qqbot_target" != *..* && "$qqbot_target" != / && "$qqbot_target" != "$HOME" ]]
[[ "$qqbot_revision" =~ ^[a-f0-9]{40}$ ]]
[[ "$(id -u)" != 0 ]]
cd -- "$qqbot_target"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
qqbot_archive="$qqbot_target/runtime/deploy/$qqbot_revision.tar.gz"
[[ -s "$qqbot_archive" ]]
mkdir -p runtime/backups
exec 9>runtime/deploy/deploy.lock
flock -n 9 || { echo '服务器上已有部署正在运行。' >&2; exit 1; }
# Stop before backing up SQLite databases and QQ login state.
if [[ -x ./bot ]] && systemctl --user cat qqbots2-astrbot.service >/dev/null 2>&1; then
  ./bot down
fi
qqbot_backup="runtime/backups/$(date -u +%Y%m%dT%H%M%SZ)-before-${qqbot_revision:0:12}.tar.gz"
if [[ -f ./bot || -d data ]]; then
  tar --exclude='./runtime/backups' --exclude='./runtime/deploy' --exclude='./.git' \
    -czf "$qqbot_backup" .
  echo "部署前备份: $qqbot_backup"
fi
# The release archive was produced by git archive; runtime files are untracked.
# Deliberately keep runtime data and locally installed plugins across deployments.
tar -xzf "$qqbot_archive" --no-same-owner --no-same-permissions
chmod +x bot scripts/*.sh
./bot setup
./bot up
qqbot_ready=false
for qqbot_attempt in $(seq 1 24); do
  echo "健康检查 $qqbot_attempt/24"
  if ./bot check; then
    qqbot_ready=true
    break
  fi
  sleep 5
done
if [[ "$qqbot_ready" != true ]]; then
  echo '部署后健康检查失败。请在服务器查看日志；已有部署的备份位于 runtime/backups/。' >&2
  exit 1
fi
printf '%s\n' "$qqbot_revision" > runtime/deployed-revision
rm -f -- "$qqbot_archive"
echo "部署成功: $qqbot_revision"
