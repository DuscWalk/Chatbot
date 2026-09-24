"""Stage pinned upstream plugins, then install while AstrBot is stopped."""

import argparse
import copy
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "runtime/plugins/build"
LOCK = ROOT / "plugins/lock.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def defaults(schema):
    return {
        k: defaults(v["items"]) if v.get("type") == "object" else copy.deepcopy(v.get("default"))
        for k, v in schema.items()
    }


def merge(base, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def build():
    BUILD.mkdir(parents=True, exist_ok=True)
    source = ROOT / "plugins/astrbot_plugin_rolebot"
    with zipfile.ZipFile(
        BUILD / "astrbot_plugin_rolebot.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                archive.write(path, path.relative_to(source.parent))
    print("Rolebot plugin archive ready.")


def stage():
    BUILD.mkdir(parents=True, exist_ok=True)
    for item in read(LOCK)["upstream"]:
        url = f"https://codeload.github.com/{item['repo']}/zip/{item['commit']}"
        path = BUILD / (item["id"] + ".zip")
        if not path.exists():
            with urllib.request.urlopen(url, timeout=90) as response:
                path.write_bytes(response.read())
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("archive checksum mismatch: " + item["id"])
    requirements = []
    for item in read(LOCK)["upstream"]:
        with zipfile.ZipFile(BUILD / (item["id"] + ".zip")) as archive:
            path = next(n for n in archive.namelist() if n.endswith("/requirements.txt"))
            requirements.append(archive.read(path).decode())
    requirements.append((ROOT / "plugins/astrbot_plugin_rolebot/requirements.txt").read_text())
    (BUILD / "requirements.txt").write_text("\n".join(requirements) + "\n")
    print("Pinned plugin archives and dependency manifest ready.")


def extract(raw, target, strip_root=True):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for member in archive.infolist():
            path = Path(member.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or (member.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("unsafe archive member")
            relative = Path(*path.parts[1:]) if strip_root else path
            if member.is_dir() or not relative.parts:
                continue
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(member))


def install(*, backup_dir=None, skip_dependencies=False):
    if (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "qqbots2-astrbot.service"]
        ).returncode
        == 0
    ):
        raise RuntimeError("Stop AstrBot before installing; leave NapCat running.")
    lock = read(LOCK)
    # Validate every archive before making changes.
    for item in lock["upstream"]:
        archive = BUILD / (item["id"] + ".zip")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("archive checksum mismatch")
    own_archive = ROOT / "runtime/lemuen/build/astrbot_plugin_lemuen.zip"
    if not own_archive.exists():
        raise ValueError("Run scripts/lemuen.py build first.")
    rolebot_archive = BUILD / "astrbot_plugin_rolebot.zip"
    if not rolebot_archive.exists():
        raise ValueError("Run scripts/manage_plugins.py build first.")
    activation = ROOT / "runtime/plugins/activation.json"
    previous_revision = read(activation).get("profile_revision", 0) if activation.exists() else 0
    targets = []
    if previous_revision < 2:
        main = read(ROOT / "data/cmd_config.json")
        profiles = [
            p
            for p in (ROOT / "data/config").glob("abconf*.json")
            if read(p).get("provider_settings", {}).get("default_personality") == "蕾缪安"
        ]
        if len(profiles) != 1:
            raise ValueError("Expected exactly one Lemuen private profile.")
        profile_path = profiles[0] if profiles else None
        profile = read(profile_path) if profile_path else {}
        # Read routing metadata only, not message content. Never print account IDs.
        with sqlite3.connect(f"file:{ROOT / 'data/data_v4.db'}?mode=ro", uri=True) as db:
            targets = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT user_id FROM conversations WHERE user_id LIKE 'napcat:FriendMessage:%'"
                )
            ]
        old_config = ROOT / "data/config/astrbot_plugin_lemuen_config.json"
        own = read(old_config)
        existing_targets = own.get("proactive", {}).get("targets", [])
        targets = existing_targets or targets
        if len(targets) != 1:
            raise ValueError("Set an explicit proactive recipient in Lemuen plugin configuration.")
        embedding = next(p for p in main["provider"] if p["id"] == "lemuen-embedding")
        source = {
            "id": "lemuen-dashscope",
            "type": "openai_chat_completion",
            "enable": True,
            "api_base": embedding["embedding_api_base"],
            "key": [embedding["embedding_api_key"]]
            if isinstance(embedding["embedding_api_key"], str)
            else embedding["embedding_api_key"],
        }
        vision = {
            "id": "lemuen-vision",
            "provider_source_id": source["id"],
            "enable": True,
            "model": "qwen3-vl-plus",
            "modalities": ["text", "image"],
            "custom_extra_body": {"max_tokens": 2048, "enable_thinking": False},
        }
    if backup_dir is not None:
        backup = Path(backup_dir).resolve()
        if (
            not backup.is_relative_to((ROOT / "runtime/backups").resolve())
            or not (backup / "data/cmd_config.json").is_file()
        ):
            raise ValueError("Deployment backup must already contain the stopped instance data.")
    else:
        backup = (
            ROOT / "runtime/backups" / ("plugins-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
        )
        backup.mkdir(parents=True, mode=0o700)
        for relative in [
            "data/config",
            "data/plugins",
            "data/plugin_data",
            "data/cmd_config.json",
            "data/data_v4.db",
        ]:
            src, dest = ROOT / relative, backup / relative
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)
        freeze = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
        (backup / "pip-freeze.txt").write_text(freeze)
    for item in lock["upstream"]:
        target = ROOT / "data/plugins" / item["id"]
        if target.exists():
            shutil.rmtree(target)  # Exact managed plugin only; data lives in plugin_data.
        extract((BUILD / (item["id"] + ".zip")).read_bytes(), target)
        if not skip_dependencies:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    str(target / "requirements.txt"),
                ],
                check=True,
            )
        cfg_path = ROOT / "data/config" / (item["id"] + "_config.json")
        config = merge(defaults(read(target / "_conf_schema.json")), item["config"])
        if cfg_path.exists():
            merge(config, read(cfg_path))
        if previous_revision < 2 and item["id"] == "astrbot_plugin_continuous_message":
            # One-time handover from caption-only to the new evidence pipeline.
            config["image_vision"]["private_image_caption_provider_id"] = ""
            config["image_handling"]["enable_image_localization"] = False
        write(cfg_path, config)
    extract(own_archive.read_bytes(), ROOT / "data/plugins", strip_root=False)
    rolebot_target = ROOT / "data/plugins/astrbot_plugin_rolebot"
    if rolebot_target.exists():
        shutil.rmtree(rolebot_target)
    extract(rolebot_archive.read_bytes(), ROOT / "data/plugins", strip_root=False)
    if not skip_dependencies:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(rolebot_target / "requirements.txt"),
            ],
            check=True,
        )
    rolebot_path = ROOT / "data/config/astrbot_plugin_rolebot_config.json"
    rolebot_config = defaults(read(rolebot_target / "_conf_schema.json"))
    if rolebot_path.exists():
        merge(rolebot_config, read(rolebot_path))
    write(rolebot_path, rolebot_config)
    if previous_revision < 2:
        for cfg in [main, profile]:
            if not any(p["id"] == source["id"] for p in cfg["provider_sources"]):
                cfg["provider_sources"].append(source)
            if not any(p["id"] == vision["id"] for p in cfg["provider"]):
                cfg["provider"].append(vision)
            for provider in cfg["provider"]:
                if provider["id"] == vision["id"]:
                    extra = provider.setdefault("custom_extra_body", {})
                    extra["max_tokens"] = max(2048, int(extra.get("max_tokens", 1024)))
            enabled = cfg.get("plugin_set", [])
            additions = ["astrbot_plugin_lemuen", "astrbot_plugin_rolebot"]
            if cfg is profile:
                additions += [p["runtime_name"] for p in lock["upstream"]]
            if enabled != ["*"]:
                cfg["plugin_set"] = list(dict.fromkeys(enabled + additions))
        if "proactive" not in own:
            own["proactive"] = {
                "enabled": True,
                "targets": targets,
                "weekdays": [1, 3, 6],
                "start_hour": 19,
                "end_hour": 21,
                "idle_minutes": 120,
            }
        write(ROOT / "data/cmd_config.json", main)
        if profile_path:
            write(profile_path, profile)
        write(old_config, own)
    prompts = ROOT / "data/plugin_data/astrbot_plugin_livingmemory/prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for name, content in lock["memory_prompts"].items():
        (prompts / (name + ".txt")).write_text(content, encoding="utf-8")
    write(
        ROOT / "runtime/plugins/activation.json",
        {
            "backup": str(backup),
            "versions": {p["id"]: p["version"] for p in lock["upstream"]},
            "lemuen": "0.2.0",
            "rolebot": "0.1.0",
            "profile_revision": 2,
            "scope": "private",
            "proactive_recipients": (
                len(targets)
                if previous_revision < 2
                else read(activation).get("proactive_recipients", 0)
            ),
        },
    )
    print("Installed private plugins; backup:", backup)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "stage", "install"])
    parser.add_argument(
        "--backup-dir", type=Path, help="Reuse an existing complete deployment backup"
    )
    parser.add_argument(
        "--skip-dependencies",
        action="store_true",
        help="Dependencies were installed by the deployment transaction",
    )
    args = parser.parse_args()
    if args.command == "install":
        install(backup_dir=args.backup_dir, skip_dependencies=args.skip_dependencies)
    else:
        {"build": build, "stage": stage}[args.command]()
