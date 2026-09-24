"""Configure the existing Lemuen instance for all QQ groups; run with AstrBot stopped."""

import argparse
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUP_ROUTE = "napcat:GroupMessage:*"
GROUP_NAME = "蕾缪安 · QQ群聊"
GROUP_SETTINGS = {
    "all_groups": True,
    "default_enabled": True,
    "probability": 3,
    "keywords": ["蕾缪安", "安姐", "枢机", "拉特兰粉发"],
    "followup_keywords": ["你", "蕾缪安"],
    "followup_seconds": 90,
    "random_cooldown_seconds": 120,
    "reply_cooldown_seconds": 3,
    "max_replies_per_minute": 6,
    "repeat_enabled": True,
    "repeat_threshold": 2,
    "repeat_window_seconds": 45,
    "repeat_cooldown_seconds": 600,
    "repeat_group_cooldown_seconds": 60,
    "quote_enabled": True,
}
CONFIG_PATHS = ("data/config", "data/cmd_config.json", "runtime/plugins/activation.json")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def preference(db, key):
    row = db.execute(
        "SELECT value FROM preferences WHERE scope='global' AND scope_id='global' AND key=?",
        (key,),
    ).fetchone()
    return json.loads(row[0]).get("val", {}) if row else {}


def set_preference(db, key, value):
    stamp = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    payload = json.dumps({"val": value}, ensure_ascii=False)
    cursor = db.execute(
        "UPDATE preferences SET value=?, updated_at=? "
        "WHERE scope='global' AND scope_id='global' AND key=?",
        (payload, stamp, key),
    )
    if not cursor.rowcount:
        db.execute(
            "INSERT INTO preferences (scope,scope_id,key,value,created_at,updated_at) "
            "VALUES ('global','global',?,?,?,?)",
            (key, payload, stamp, stamp),
        )


def profile_path(root, metadata):
    name = metadata["path"]
    if Path(name).name != name or not name.startswith("abconf_") or not name.endswith(".json"):
        raise ValueError("Invalid native profile filename")
    return root / "data/config" / name


def configure(root):
    """Explicit operation only; future deployments preserve all WebUI choices."""
    with sqlite3.connect(root / "data/data_v4.db") as db:
        mapping = preference(db, "abconf_mapping")
        routes = preference(db, "umop_config_routing")
        private_id = routes.get("napcat:FriendMessage:*")
        if private_id not in mapping:
            raise ValueError("An existing Lemuen private profile is required")
        private = read(profile_path(root, mapping[private_id]))
        if private.get("provider_settings", {}).get("default_personality") != "蕾缪安":
            raise ValueError("Expected the Lemuen private persona")
        old_group_id = routes.get(GROUP_ROUTE)
        if old_group_id is not None and mapping.get(old_group_id, {}).get("name") != GROUP_NAME:
            raise ValueError("The group route is already bound to an unmanaged profile")
        group_id = old_group_id or str(uuid.uuid4())
        filename = "abconf_" + group_id + ".json"
        group = (
            read(profile_path(root, mapping[group_id])) if old_group_id else copy.deepcopy(private)
        )
        group["provider_settings"].update(
            enable=True,
            default_provider_id=private["provider_settings"]["default_provider_id"],
            default_personality="蕾缪安",
            max_context_length=24,
        )
        group["plugin_set"] = ["astrbot_plugin_lemuen", "astrbot_plugin_rolebot"]
        group["disable_builtin_commands"] = True
        settings = group["platform_settings"]
        settings.update(
            unique_session=False,
            ignore_bot_self_message=True,
            ignore_at_all=True,
            reply_with_mention=False,
            reply_with_quote=False,
        )
        # AstrBot counts all incoming messages before Rolebot routing. Limit actual
        # bot replies in Rolebot instead, so busy groups can still @ the bot.
        settings["rate_limit"] = {"time": 60, "count": 0, "strategy": "discard"}
        main_path = root / "data/cmd_config.json"
        main = read(main_path)
        main["platform_settings"]["unique_session"] = False
        own_path = root / "data/config/astrbot_plugin_lemuen_config.json"
        own = read(own_path)
        own["allowed_sessions"] = list(
            dict.fromkeys([*own.get("allowed_sessions", []), GROUP_ROUTE])
        )
        rolebot_path = root / "data/config/astrbot_plugin_rolebot_config.json"
        rolebot = read(rolebot_path)
        rolebot.setdefault("groups", {}).update(GROUP_SETTINGS)
        rolebot["groups"]["blacklist"] = []
        mapping[group_id] = {"path": filename, "name": GROUP_NAME}
        # First matching route wins. This narrow rule also precedes broad defaults.
        routes = {GROUP_ROUTE: group_id, **{k: v for k, v in routes.items() if k != GROUP_ROUTE}}
        for path, value in (
            (root / "data/config" / filename, group),
            (main_path, main),
            (own_path, own),
            (rolebot_path, rolebot),
        ):
            write(path, value)
        set_preference(db, "abconf_mapping", mapping)
        set_preference(db, "umop_config_routing", routes)
        marker = root / "runtime/plugins/activation.json"
        if marker.exists():
            activation = read(marker)
            activation["scope"] = "private_and_groups"
            write(marker, activation)
    return {"group_profile": GROUP_NAME, "routing": GROUP_ROUTE, "settings": GROUP_SETTINGS}


def apply(root):
    if (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "qqbots2-astrbot.service"]
        ).returncode
        == 0
    ):
        raise RuntimeError("Stop AstrBot before configuring groups; leave NapCat running")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "runtime/backups" / ("groups-" + stamp)
    backup.mkdir(parents=True, mode=0o700)
    for relative in CONFIG_PATHS:
        source, dest = root / relative, backup / relative
        if source.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, dest) if source.is_dir() else shutil.copy2(source, dest)
    with sqlite3.connect(root / "data/data_v4.db") as source:
        with sqlite3.connect(backup / "data/data_v4.db") as dest:
            source.backup(dest)
    try:
        result = configure(root)
    except BaseException:
        for relative in CONFIG_PATHS:
            path, saved = root / relative, backup / relative
            if saved.is_dir():
                shutil.rmtree(path)
                shutil.copytree(saved, path)
            elif saved.exists():
                shutil.copy2(saved, path)
        with sqlite3.connect(backup / "data/data_v4.db") as source:
            with sqlite3.connect(root / "data/data_v4.db") as dest:
                source.backup(dest)
        raise
    print(json.dumps({**result, "backup": str(backup)}, ensure_ascii=False))


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply all-group settings with backup")
    args = parser.parse_args()
    if args.apply:
        apply(ROOT)
    else:
        print(
            json.dumps(
                {"group_profile": GROUP_NAME, "settings": GROUP_SETTINGS}, ensure_ascii=False
            )
        )
