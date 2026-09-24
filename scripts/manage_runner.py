"""Prepare/register the repository's deployment-only GitHub Actions runner."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "2.337.0"
SHA256 = "70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613"
REPOSITORY = "DuscWalk/Chatbot"
LABEL = "chatbot-njuse-deploy"
SERVICE = "qqbots2-actions-runner.service"
RUNNER = Path.home() / "actions-runner-chatbot"


JOB_GATE = """#!/usr/bin/env bash
set -euo pipefail
[[ "${GITHUB_REPOSITORY:-}" == DuscWalk/Chatbot ]]
[[ "${GITHUB_REF:-}" == refs/heads/main ]]
[[ "${GITHUB_WORKFLOW_REF:-}" == DuscWalk/Chatbot/.github/workflows/ci-cd.yml@refs/heads/main ]]
[[ "${GITHUB_EVENT_NAME:-}" == push || "${GITHUB_EVENT_NAME:-}" == workflow_dispatch ]]
"""


def run(args, **kwargs):
    return subprocess.run(args, check=True, cwd=RUNNER, **kwargs)


def prepare():
    if os.getuid() == 0:
        raise RuntimeError("Run as the ordinary deployment user.")
    os.umask(0o077)
    RUNNER.mkdir(mode=0o700, exist_ok=True)
    RUNNER.chmod(0o700)
    if not (RUNNER / "bin/Runner.Listener").exists():
        archive = RUNNER / "runner.tar.gz"
        url = f"https://github.com/actions/runner/releases/download/v{VERSION}/actions-runner-linux-x64-{VERSION}.tar.gz"
        if not archive.exists():
            partial = archive.with_suffix(".partial")
            try:
                with (
                    urllib.request.urlopen(url, timeout=25) as response,
                    partial.open("wb") as dest,
                ):
                    while chunk := response.read(1024 * 1024):
                        dest.write(chunk)
                partial.replace(archive)
            finally:
                partial.unlink(missing_ok=True)
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256")
        if digest.hexdigest() != SHA256:
            archive.unlink(missing_ok=True)
            raise ValueError("Runner archive checksum mismatch")
        with tarfile.open(archive) as release:
            release.extractall(RUNNER, filter="data")
        archive.unlink()
    # Same layout as svc.sh: runsvc.sh expects to run from the runner root.
    import shutil

    shutil.copy2(RUNNER / "bin/runsvc.sh", RUNNER / "runsvc.sh")
    version = run(
        [str(RUNNER / "bin/Runner.Listener"), "--version"], capture_output=True, text=True
    ).stdout.strip()
    # This hook lives outside the checkout and runs before any workflow step.
    # It rejects pull requests/forks even if their workflow asks for this label.
    hook = RUNNER / "deployment-job-gate.sh"
    hook.write_text(JOB_GATE)
    hook.chmod(0o700)
    env_file = RUNNER / ".env"
    previous = env_file.read_text().splitlines() if env_file.exists() else []
    lines = [line for line in previous if not line.startswith("ACTIONS_RUNNER_HOOK_JOB_STARTED=")]
    lines.append("ACTIONS_RUNNER_HOOK_JOB_STARTED=" + str(hook))
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    unit = Path.home() / ".config/systemd/user" / SERVICE
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"""[Unit]
Description=Chatbot GitHub deployment runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={RUNNER}
ExecStart={RUNNER}/runsvc.sh
Restart=always
RestartSec=10
KillMode=process
TimeoutStopSec=300
UMask=0077
Environment=XDG_RUNTIME_DIR=/run/user/{os.getuid()}

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(
        json.dumps(
            {
                "prepared": True,
                "runner_version": version,
                "registered": (RUNNER / ".runner").exists(),
                "label": LABEL,
            }
        )
    )


def register():
    if not (RUNNER / "config.sh").exists():
        raise RuntimeError("Run prepare first")
    if not (RUNNER / ".runner").exists():
        token = sys.stdin.read().strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError("Provide the registration token on stdin")
        env = os.environ.copy()
        env["ACTIONS_RUNNER_INPUT_TOKEN"] = token
        # Keep token out of argv, shell history, public logs and checked-in files.
        result = subprocess.run(
            [
                str(RUNNER / "config.sh"),
                "--unattended",
                "--url",
                "https://github.com/" + REPOSITORY,
                "--name",
                "qqbots2-" + socket.gethostname(),
                "--no-default-labels",
                "--labels",
                LABEL,
                "--work",
                "_work",
            ],
            cwd=RUNNER,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            message = (result.stdout + result.stderr).replace(token, "[redacted]")
            (RUNNER / "registration-error.log").write_text(message)
            (RUNNER / "registration-error.log").chmod(0o600)
            raise RuntimeError("Runner registration failed; private diagnostic saved on the runner")
        env.pop("ACTIONS_RUNNER_INPUT_TOKEN", None)
    for name in [".runner", ".credentials", ".credentials_rsaparams"]:
        if (RUNNER / name).exists():
            (RUNNER / name).chmod(0o600)
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE], check=True)
    print("Deployment runner registered and started.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "register"])
    args = parser.parse_args()
    {"prepare": prepare, "register": register}[args.command]()
