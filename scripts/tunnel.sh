#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 ]]; then
  echo '用法: ./scripts/tunnel.sh 用户@服务器 [SSH 参数，例如 -p 2222]' >&2
  exit 2
fi
qqbot_server="$1"
shift
qqbot_astrbot_port="${QQBOT_LOCAL_ASTRBOT_PORT:-16185}"
qqbot_napcat_port="${QQBOT_LOCAL_NAPCAT_PORT:-16099}"
echo "AstrBot: http://127.0.0.1:${qqbot_astrbot_port}"
echo "NapCat:  http://127.0.0.1:${qqbot_napcat_port}/webui/"
echo "抽卡后台: http://127.0.0.1:${QQBOT_LOCAL_CARDDRAW_PORT:-13100}/admin"
echo '保持此终端打开；Ctrl+C 关闭隧道，服务器上的机器人继续运行。'
exec ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L "127.0.0.1:${qqbot_astrbot_port}:127.0.0.1:6185" \
  -L "127.0.0.1:${qqbot_napcat_port}:127.0.0.1:6099" \
  -L "127.0.0.1:${QQBOT_LOCAL_CARDDRAW_PORT:-13100}:127.0.0.1:3100" \
  "$@" "$qqbot_server"
