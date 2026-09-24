#!/usr/bin/env bash
# Executed on Ubuntu as the ordinary deployment user. NapCat remains running.
set -euo pipefail
umask 077
qqbot_target="${1:?project directory required}"
qqbot_revision="${2:?revision required}"
[[ "$qqbot_target" =~ ^/[a-zA-Z0-9_./-]+$ && "$qqbot_target" != *..* && "$qqbot_target" != / && "$qqbot_target" != "$HOME" ]]
[[ "$qqbot_revision" =~ ^[a-f0-9]{40}$ && "$(id -u)" != 0 ]]
[[ -f "$qqbot_target/data/cmd_config.json" ]]
qqbot_uid="$(id -u)"
export XDG_RUNTIME_DIR="/run/user/$qqbot_uid"
unset DBUS_SESSION_BUS_ADDRESS
qqbot_python="${DEPLOY_PYTHON:-}"
if [[ ! -x "$qqbot_python" ]]; then
  for qqbot_prefix in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3"; do
    if [[ -x "$qqbot_prefix/envs/astrbot-wsl/bin/python" ]]; then
      qqbot_python="$qqbot_prefix/envs/astrbot-wsl/bin/python"
      break
    fi
  done
fi
[[ -x "$qqbot_python" ]] || { echo 'astrbot-wsl Python unavailable' >&2; exit 1; }
qqbot_archive="$qqbot_target/runtime/deploy/$qqbot_revision.tar.gz"
[[ -s "$qqbot_archive" ]]
qqbot_candidate="$(mktemp -d "$qqbot_target/runtime/deploy/candidate-XXXXXX")"
trap 'rm -rf -- "$qqbot_candidate"' EXIT
"$qqbot_python" - "$qqbot_archive" "$qqbot_candidate" <<'PYTHON'
import sys,tarfile
from pathlib import Path
with tarfile.open(sys.argv[1]) as archive:
    for member in archive.getmembers():
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
            raise ValueError("Unsafe release member")
        if path.parts[0] in {".git", ".env", "data", "runtime"}:
            raise ValueError("Runtime data cannot be deployed from Git")
    archive.extractall(sys.argv[2], filter="data")
PYTHON
"$qqbot_python" "$qqbot_candidate/scripts/deploy_release.py" --source "$qqbot_candidate" --target "$qqbot_target" --revision "$qqbot_revision"
rm -f -- "$qqbot_archive"
