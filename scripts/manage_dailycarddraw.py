"""Deploy the pinned daily-card backend independently of NapCat and AstrBot."""

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import tarfile
import urllib.request
from collections import Counter
from datetime import UTC, datetime
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
    # The deployment service uses umask 077. Public source must remain readable by
    # the Node/MySQL users inside containers; secrets live outside this build tree.
    build.chmod(0o755)
    for path in build.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
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


def load_catalog(root=ROOT):
    """Validate the tracked catalog and every referenced avatar/icon before importing."""
    catalog = read(root / "deploy/dailycarddraw/catalog.json")
    cards = catalog["cards"]
    weights = catalog["rarity_weights"]
    if catalog["schema_version"] != 1 or not cards:
        raise ValueError("Unsupported or empty card catalog")
    if set(weights) != {str(n) for n in range(1, 7)} or any(
        type(value) is not int or value <= 0 for value in weights.values()
    ):
        raise ValueError("All six rarities need positive integer weights")
    for field in ("card_key", "name", "game_char_id"):
        values = [card[field] for card in cards]
        if any(not value for value in values) or len(set(values)) != len(cards):
            raise ValueError(f"Empty or duplicate catalog field: {field}")
    professions = {"先锋", "狙击", "重装", "医疗", "辅助", "术师", "特种", "近卫"}
    resources = set()
    for card in cards:
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", card["card_key"])
            or type(card["rarity"]) is not int
            or str(card["rarity"]) not in weights
            or card["profession"] not in professions
            or not isinstance(card["obtain"], list)
            or any(not isinstance(value, str) for value in card["obtain"])
        ):
            raise ValueError(f"Invalid card: {card['card_key']}")
        resources.update(
            {
                f"resource/avatar/{card['card_key']}.png",
                f"resource/profession/{card['profession']}.png",
                f"resource/rarity/rarity{card['rarity']}.png",
            }
        )
    pinned = next(p for p in read(root / "plugins/lock.json")["upstream"] if p["id"] == PLUGIN)
    source = catalog["avatar_source"]
    if source["commit"] != pinned["commit"] or source["archive_sha256"] != pinned["sha256"]:
        raise ValueError("Catalog avatars do not match the pinned plugin")
    with tarfile.open(root / "runtime/dailycarddraw/build/server/resource.tar.gz") as archive:
        members = {str(Path(member.name)): member for member in archive.getmembers()}
        for name in sorted(resources):
            member = members.get(name)
            if member is None or not member.isfile():
                raise ValueError(f"Missing card image: {name}")
            with archive.extractfile(member) as image:
                header = image.read(24)
            if (
                len(header) != 24
                or header[:8] != b"\x89PNG\r\n\x1a\n"
                or header[12:16] != b"IHDR"
                or not int.from_bytes(header[16:20], "big")
                or not int.from_bytes(header[20:24], "big")
            ):
                raise ValueError(f"Invalid PNG: {name}")
    return catalog


def panel_client(root=ROOT):
    credentials = read(root / "runtime/dailycarddraw/credentials.json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token = ""

    def request(path, body=None, method=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(
            "http://127.0.0.1:3100/manage/api" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        with opener.open(req, timeout=30) as response:
            result = json.load(response)
        if not result.get("success"):
            raise RuntimeError(f"Card management API failed: {path}")
        return result["data"]

    token = request(
        "/login",
        {"username": credentials["panel_username"], "password": credentials["panel_password"]},
    )["token"]
    return request


def import_catalog(root=ROOT):
    """Explicit operator action only: replace pool contents, retaining IDs/history/quotas."""
    stage(root)
    catalog = load_catalog(root)
    request = panel_client(root)
    pool = next(
        (p for p in request("/pools")["list"] if p["pool_key"] == catalog["pool"]["pool_key"]),
        None,
    )
    if pool is None:
        raise ValueError("Target pool must already exist")
    path = f"/pools/{pool['id']}"
    before = request(path + "/cards")
    old_cards = request("/cards")["list"]
    runtime = root / "runtime/dailycarddraw"
    write(
        runtime / "catalog-before-import.json",
        {
            "saved_at": datetime.now(UTC).isoformat(),
            "cards": old_cards,
            "pool": pool,
            "items": [
                {key: row[key] for key in ("card_id", "weight", "is_up")} for row in before["list"]
            ],
            "rarity_items": [
                {key: row[key] for key in ("rarity", "weight")} for row in before["rarity_list"]
            ],
        },
    )
    imported = request("/cards/import", {"cards": catalog["cards"]})
    if imported["skipped"] or imported["errors"] or imported["total"] != len(catalog["cards"]):
        raise RuntimeError("Incomplete card import; pool membership has not been changed")
    saved = {card["card_key"]: card for card in request("/cards")["list"]}
    for old in old_cards:
        current = saved[old["card_key"]]
        if any(current[key] != old[key] for key in ("id", "description", "is_enabled")):
            raise RuntimeError("Import changed an existing card ID or manual setting")
    for card in catalog["cards"]:
        current = saved[card["card_key"]]
        if current["card_name"] != card["name"] or any(
            current[key] != card[key] for key in ("rarity", "profession", "obtain")
        ):
            raise RuntimeError(f"Imported card differs from catalog: {card['card_key']}")
    request(
        path + "/cards",
        {
            "items": [
                {"card_id": saved[card["card_key"]]["id"], "weight": 1, "is_up": False}
                for card in catalog["cards"]
            ],
            "rarity_items": [
                {"rarity": int(rarity), "weight": weight}
                for rarity, weight in catalog["rarity_weights"].items()
            ],
        },
        method="PUT",
    )
    metadata = {key: catalog["pool"][key] for key in ("pool_name", "description")}
    request(path, metadata, method="PUT")
    after = request(path + "/cards")
    expected = {card["card_key"] for card in catalog["cards"]}
    if (
        len(after["list"]) != len(expected)
        or {card["card_key"] for card in after["list"]} != expected
        or any(card["weight"] != 1 or card["is_up"] for card in after["list"])
        or {str(row["rarity"]): row["weight"] for row in after["rarity_list"]}
        != catalog["rarity_weights"]
        or after["pool"] != pool | metadata
    ):
        raise RuntimeError("Pool verification failed; inspect catalog-before-import.json")
    summary = {
        "imported_at": datetime.now(UTC).isoformat(),
        "data_date": catalog["data_date"],
        "pool_id": pool["id"],
        "pool_name": metadata["pool_name"],
        "cards": len(after["list"]),
        "enabled_cards": sum(bool(card["is_enabled"]) for card in after["list"]),
        "by_rarity": dict(sorted(Counter(card["rarity"] for card in after["list"]).items())),
        "rarity_weights": catalog["rarity_weights"],
        "created": imported["created"],
        "updated": imported["updated"],
        "existing_ids_and_quota_settings_preserved": True,
    }
    write(runtime / "latest-import.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "stage",
            "prepare",
            "build",
            "up",
            "restart",
            "check",
            "check-catalog",
            "import-catalog",
        ],
    )
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
    elif args.command == "check-catalog":
        stage()
        catalog = load_catalog()
        print(f"Catalog valid: {len(catalog['cards'])} cards, all avatars and icons present.")
    elif args.command == "import-catalog":
        import_catalog()
    else:
        health()


if __name__ == "__main__":
    main()
