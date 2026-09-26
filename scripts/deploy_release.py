"""Update an initialized AstrBot instance; keep NapCat and WebUI choices intact."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SERVICE = "qqbots2-astrbot.service"
DATA_PATHS = [
    "data/config",
    "data/plugins",
    "data/plugin_data",
    "data/cmd_config.json",
    "data/data_v4.db",
    "data/data_v4.db-wal",
    "data/data_v4.db-shm",
    "data/knowledge_base",
    "runtime/plugins/build",
    "runtime/lemuen/build",
    "runtime/plugins/activation.json",
    "runtime/deployed-revision",
    "runtime/deployed-files.json",
    "runtime/dailycarddraw/compose.env",
    "runtime/dailycarddraw/configured.json",
]


def copy_path(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def source_files(root):
    paths = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and not any(
            x in p.relative_to(root).parts
            for x in (".git", "__pycache__", "runtime", "data", ".env")
        )
    ]
    return sorted(str(p.relative_to(root)) for p in paths)


def snapshot(target, backup, files):
    backup.mkdir(parents=True, mode=0o700)
    present = []
    for relative in [*files, *DATA_PATHS]:
        source = target / relative
        if source.exists() or source.is_symlink():
            copy_path(source, backup / relative)
            present.append(relative)
    (backup / "manifest.json").write_text(json.dumps({"files": files, "present": present}) + "\n")


def restore(target, backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    for relative in [*manifest["files"], *DATA_PATHS]:
        remove_path(target / relative)
        if relative in manifest["present"]:
            copy_path(backup / relative, target / relative)


class Deployment:
    def __init__(self, source, target, revision, python):
        self.source = source.resolve()
        self.target = target.resolve()
        self.revision = revision
        self.python = python
        self.log = self.target / "runtime/deploy/latest.log"
        self.backup = None
        self.started = None
        self.pending = []
        self.previous = []
        self.introduced = []
        self.dependencies_touched = False

    def run(self, args, *, cwd=None):
        with self.log.open("a") as output:
            result = subprocess.run(
                args, cwd=cwd or self.source, stdout=output, stderr=subprocess.STDOUT
            )
        if result.returncode:
            raise RuntimeError(
                "Deployment step failed: "
                + Path(str(args[0])).name
                + "; see runtime/deploy/latest.log"
            )

    def service(self, action):
        self.run(["systemctl", "--user", action, SERVICE])

    def prepare(self):
        if self.source == self.target or not (self.target / "data/cmd_config.json").is_file():
            raise ValueError("Deployment requires a separate checkout and an initialized target")
        if len(self.revision) != 40 or any(c not in "0123456789abcdef" for c in self.revision):
            raise ValueError("Invalid revision")
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("")
        # Reuse verified pinned archives; ordinary updates need no additional GitHub downloads.
        lock = json.loads((self.source / "plugins/lock.json").read_text())
        build = self.source / "runtime/plugins/build"
        build.mkdir(parents=True, exist_ok=True)
        for item in lock["upstream"]:
            cached = self.target / "runtime/plugins/build" / (item["id"] + ".zip")
            if (
                cached.exists()
                and hashlib.sha256(cached.read_bytes()).hexdigest() == item["sha256"]
            ):
                shutil.copy2(cached, build / cached.name)
        for args in [
            ("scripts/lemuen.py", "build"),
            ("scripts/manage_plugins.py", "build"),
            ("scripts/manage_plugins.py", "stage"),
        ]:
            self.run([self.python, *args])
        if (self.source / "scripts/manage_dailycarddraw.py").exists():
            self.run([self.python, "scripts/manage_dailycarddraw.py", "build"])
        # Resolve only missing/changed packages; normal deployments download none.
        plan = self.source / "runtime/deploy/dependencies.json"
        plan.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                self.python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--dry-run",
                "--report",
                str(plan),
                "-r",
                "requirements.txt",
                "-r",
                "runtime/plugins/build/requirements.txt",
            ]
        )
        changes = json.loads(plan.read_text()).get("install", [])
        installed = json.loads(
            subprocess.check_output([self.python, "-m", "pip", "list", "--format=json"], text=True)
        )
        current = {re.sub(r"[-_.]+", "-", p["name"]).lower(): p["version"] for p in installed}
        for change in changes:
            meta = change["metadata"]
            name = re.sub(r"[-_.]+", "-", meta["name"]).lower()
            self.pending.append(name + "==" + meta["version"])
            if name in current:
                self.previous.append(name + "==" + current[name])
            else:
                self.introduced.append(name)
        for versions in (self.pending, self.previous):
            if versions:
                self.run(
                    [
                        self.python,
                        "-m",
                        "pip",
                        "wheel",
                        "--disable-pip-version-check",
                        "--no-deps",
                        "--wheel-dir",
                        str(self.source / "runtime/deploy/wheels"),
                        *versions,
                    ]
                )

    def install_dependencies(self):
        if self.pending:
            self.dependencies_touched = True
            self.run(
                [
                    self.python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--no-index",
                    "--find-links",
                    str(self.source / "runtime/deploy/wheels"),
                    *self.pending,
                ]
            )

    def restore_dependencies(self):
        if not self.dependencies_touched:
            return
        if self.previous:
            self.run(
                [
                    self.python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--no-index",
                    "--find-links",
                    str(self.source / "runtime/deploy/wheels"),
                    *self.previous,
                ]
            )
        if self.introduced:
            self.run([self.python, "-m", "pip", "uninstall", "--yes", *self.introduced])

    def health(self):
        cfg = json.loads((self.target / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
        port = cfg["dashboard"]["port"]
        platform = next(p for p in cfg["platform"] if p["id"] == "napcat")
        onebot = platform["ws_reverse_port"]
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            service = (
                subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE]).returncode
                == 0
            )
            connection = subprocess.run(
                ["ss", "-Hnt", "state", "established", f"( sport = :{onebot} )"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                    ready = response.status == 200
            except OSError:
                ready = False
            if service and connection and ready:
                # Only inspect startup metadata. Never print chat history or IDs.
                logs = subprocess.check_output(
                    [
                        "journalctl",
                        "--user",
                        "-u",
                        SERVICE,
                        "--since",
                        self.started,
                        "--no-pager",
                        "-o",
                        "cat",
                    ],
                    text=True,
                )
                if "Traceback (most recent call last)" in logs or "[ERROR]" in logs:
                    raise RuntimeError(
                        "AstrBot startup logged an error; inspect the private service log"
                    )
                return
            time.sleep(2)
        raise RuntimeError("AstrBot/OneBot health check timed out")

    def activate(self):
        files = source_files(self.source)
        marker = self.target / "runtime/deployed-files.json"
        old_files = json.loads(marker.read_text()) if marker.exists() else []
        # Only prune names from a previous deployment manifest, never unknown user files.
        all_files = sorted(set(files) | set(old_files))
        if any(
            Path(p).is_absolute()
            or ".." in Path(p).parts
            or Path(p).parts[0] in {"data", "runtime", ".git", ".env"}
            for p in all_files
        ):
            raise ValueError("Invalid deployment file manifest")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.backup = (
            self.target / "runtime/backups" / ("deploy-" + stamp + "-" + self.revision[:7])
        )
        self.service("stop")
        snapshotted = False
        try:
            snapshot(self.target, self.backup, all_files)
            snapshotted = True
            freeze = subprocess.check_output([self.python, "-m", "pip", "freeze"], text=True)
            (self.backup / "pip-freeze.txt").write_text(freeze)
            self.install_dependencies()
            self.run([self.python, "scripts/plugins_smoke.py"])
            for relative in files:
                destination = self.target / relative
                if destination.is_symlink():
                    destination.unlink()
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.source / relative, destination)
            for relative in set(old_files) - set(files):
                remove_path(self.target / relative)
            for folder in ("runtime/lemuen/build", "runtime/plugins/build"):
                dest = self.target / folder
                remove_path(dest)
                shutil.copytree(self.source / folder, dest)
            if (self.target / "scripts/manage_dailycarddraw.py").exists():
                self.run([self.python, "scripts/manage_dailycarddraw.py", "up"], cwd=self.target)
            self.run(
                [
                    self.python,
                    "scripts/manage_plugins.py",
                    "install",
                    "--backup-dir",
                    str(self.backup),
                    "--skip-dependencies",
                ],
                cwd=self.target,
            )
            self.started = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            self.service("start")
            self.health()
            marker.write_text(json.dumps(files) + "\n")
            (self.target / "runtime/deployed-revision").write_text(self.revision + "\n")
            activation = self.target / "runtime/plugins/activation.json"
            value = json.loads(activation.read_text())
            value.update(source_revision=self.revision, napcat_restarted=False)
            activation.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        except BaseException:
            self.service("stop")
            try:
                if snapshotted:
                    restore(self.target, self.backup)
                    if (self.target / "runtime/dailycarddraw/compose.env").exists():
                        self.run(
                            [self.python, "scripts/manage_dailycarddraw.py", "restart"],
                            cwd=self.target,
                        )
                self.restore_dependencies()
            finally:
                self.service("start")
            raise
        print("Deployment complete:", self.revision[:12], "; backup:", self.backup)

    def execute(self):
        if os.getuid() == 0:
            raise ValueError("Do not deploy as root")
        self.target.joinpath("runtime/deploy").mkdir(parents=True, exist_ok=True)
        with (self.target / "runtime/deploy/deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.prepare()
            self.activate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    Deployment(args.source, args.target, args.revision, sys.executable).execute()
