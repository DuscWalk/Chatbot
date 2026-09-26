"""Deploy the pinned daily-card backend independently of NapCat and AstrBot."""

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

if __package__:
    from .manage_plugins import ROOT, defaults, extract, read, write
else:
    from manage_plugins import ROOT, defaults, extract, read, write

PLUGIN = "astrbot_plugin_dailycarddraw"


def stage(root=ROOT):
    item = next(p for p in read(root / "plugins/lock.json")["upstream"] if p["id"] == PLUGIN)
    archive = root / "runtime/plugins/build" / (PLUGIN + ".zip")
    raw = archive.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError("Daily-card archive checksum mismatch")
    support = root / "deploy/dailycarddraw"
    digest = hashlib.sha256(raw)
    for name in ("Dockerfile", "package-lock.json", "backend.patch"):
        digest.update((support / name).read_bytes())
    tag = "qqbots2-dailycarddraw:" + digest.hexdigest()[:16]
    runtime = root / "runtime/dailycarddraw"
    build = runtime / "build"
    marker = build / ".image"
    if not marker.exists() or marker.read_text().strip() != tag:
        if build.exists():
            shutil.rmtree(build)
        extract(raw, build)
        # The upstream resource archive is consumed by Docker's tar, so validate it first.
        with tarfile.open(build / "server/resource.tar.gz") as resource:
            for member in resource.getmembers():
                path = Path(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("Unsafe daily-card resource archive")
        shutil.copy2(support / "package-lock.json", build / "server/package-lock.json")
        shutil.copy2(support / "Dockerfile", build / "Dockerfile")
        subprocess.run(
            ["patch", "--batch", "-p1", "-d", str(build / "server")],
            input=(support / "backend.patch").read_text(),
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        marker.write_text(tag + "\n")
    (runtime / "compose.env").write_text("DAILYCARDDRAW_IMAGE=" + tag + "\n")
    return tag


def prepare(root=ROOT):
    tag = stage(root)
    runtime = root / "runtime/dailycarddraw"
    runtime.chmod(0o700)
    path = runtime / "credentials.json"
    if not path.exists():
        write(
            path,
            {
                "mysql_root_password": secrets.token_hex(32),
                "mysql_password": secrets.token_hex(32),
                "api_token": secrets.token_hex(32),
                "panel_username": "admin",
                "panel_password": secrets.token_urlsafe(24),
                "panel_jwt_secret": secrets.token_hex(32),
            },
        )
    credentials = read(path)
    database = {
        "MYSQL_DATABASE": "astrbot_daily_carddraw",
        "MYSQL_USER": "daily_carddraw",
        "MYSQL_PASSWORD": credentials["mysql_password"],
    }
    envs = {
        "mysql.env": database | {"MYSQL_ROOT_PASSWORD": credentials["mysql_root_password"]},
        "backend.env": database
        | {
            "APP_ENV": "production",
            "APP_HOST": "0.0.0.0",
            "APP_PORT": "3100",
            "APP_TIMEZONE": "Asia/Shanghai",
            "MYSQL_HOST": "db",
            "MYSQL_PORT": "3306",
            "MYSQL_POOL_SIZE": "5",
            "ADMIN_API_TOKEN": credentials["api_token"],
            "PANEL_USERNAME": credentials["panel_username"],
            "PANEL_PASSWORD_SHA256": hashlib.sha256(
                credentials["panel_password"].encode()
            ).hexdigest(),
            "PANEL_JWT_SECRET": credentials["panel_jwt_secret"],
            "NODE_OPTIONS": "--max-old-space-size=160",
        },
    }
    for name, values in envs.items():
        text = "".join(f"{key}={value}\n" for key, value in values.items())
        p = runtime / name
        p.write_text(text)
        p.chmod(0o600)
    return tag


def configure_plugin(root=ROOT, enable_profiles=False):
    """Set up this instance once; retain later WebUI changes on normal deployments."""
    runtime = root / "runtime/dailycarddraw"
    credentials_path = runtime / "credentials.json"
    if credentials_path.exists() and not (runtime / "configured.json").exists():
        credentials = read(credentials_path)
        cfg_path = root / "data/config" / (PLUGIN + "_config.json")
        schema = root / "data/plugins" / PLUGIN / "_conf_schema.json"
        cfg = defaults(read(schema)) if schema.exists() else {}
        if cfg_path.exists():
            cfg.update(read(cfg_path))
        cfg.update(api_base_url="http://127.0.0.1:3100", api_token=credentials["api_token"])
        if not cfg.get("admin_qq_list"):
            cfg["admin_qq_list"] = read(root / "data/cmd_config.json").get("admins_id", [])
        cfg["debug_log_enabled"] = False
        write(cfg_path, cfg)
        write(runtime / "configured.json", {"backend": "local", "version": 1})
    if enable_profiles:
        for path in [
            root / "data/cmd_config.json",
            *sorted((root / "data/config").glob("abconf*.json")),
        ]:
            cfg = read(path)
            if (
                path.name != "cmd_config.json"
                and cfg.get("provider_settings", {}).get("default_personality") != "蕾缪安"
            ):
                continue
            enabled = cfg.get("plugin_set", [])
            if enabled != ["*"] and PLUGIN not in enabled:
                cfg["plugin_set"] = [*enabled, PLUGIN]
                write(path, cfg)


def compose(root=ROOT):
    return [
        "docker",
        "compose",
        "--env-file",
        str(root / "runtime/dailycarddraw/compose.env"),
        "-f",
        str(root / "compose.dailycarddraw.yaml"),
    ]


def build_image(root=ROOT):
    tag = stage(root)
    existing = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    if existing.returncode:
        subprocess.run(
            ["docker", "build", "-t", tag, str(root / "runtime/dailycarddraw/build")], check=True
        )
    return tag


def health(root=ROOT):
    credentials = read(root / "runtime/dailycarddraw/credentials.json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "http://127.0.0.1:3100/api/daily-carddraw/admin/pools",
        headers={"Authorization": "Bearer " + credentials["api_token"]},
    )
    with opener.open(req, timeout=10) as response:
        result = json.load(response)
    if not result.get("success"):
        raise RuntimeError("Daily-card database/API check failed")
    print("Daily-card backend and database ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["stage", "prepare", "build", "up", "restart", "check"])
    args = parser.parse_args()
    if args.command == "stage":
        print(stage())
    elif args.command == "prepare":
        print(prepare())
    elif args.command == "build":
        print(build_image())
    elif args.command in {"up", "restart"}:
        if args.command == "up":
            prepare()
            build_image()
        subprocess.run([*compose(), "up", "-d", "--wait", "--wait-timeout", "180"], check=True)
        health()
    else:
        health()


if __name__ == "__main__":
    main()
