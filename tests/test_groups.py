"""Group profile migration against synthetic config/SQLite, with rollback."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import manage_groups as groups
from scripts.manage_plugins import defaults, merge


class GroupConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "data/config"
        self.config.mkdir(parents=True)
        self.private_path = self.config / "abconf_private.json"
        self.private = {
            "provider_settings": {
                "default_personality": "蕾缪安",
                "default_provider_id": "synthetic-chat",
            },
            "platform_settings": {"unique_session": True},
            "plugin_set": ["private-memory", "private-debounce"],
        }
        groups.write(self.private_path, self.private)
        groups.write(self.root / "data/cmd_config.json", self.private)
        groups.write(
            self.config / "astrbot_plugin_lemuen_config.json",
            {
                "allowed_sessions": ["napcat:FriendMessage:*"],
                "proactive": {"targets": ["napcat:FriendMessage:synthetic"], "enabled": True},
            },
        )
        groups.write(
            self.config / "astrbot_plugin_rolebot_config.json",
            {"groups": {}, "vision": {"enabled": True}},
        )
        with sqlite3.connect(self.root / "data/data_v4.db") as db:
            db.execute(
                "CREATE TABLE preferences (scope TEXT,scope_id TEXT,key TEXT,value TEXT,created_at TEXT,updated_at TEXT)"
            )
            groups.set_preference(
                db, "abconf_mapping", {"private": {"path": self.private_path.name, "name": "私聊"}}
            )
            groups.set_preference(db, "umop_config_routing", {"napcat:FriendMessage:*": "private"})
        self.before = self.snapshot()

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in (self.root / "data").rglob("*.json")
        }

    def routing(self):
        with sqlite3.connect(self.root / "data/data_v4.db") as db:
            return groups.preference(db, "umop_config_routing"), groups.preference(
                db, "abconf_mapping"
            )

    def test_all_group_profile_isolated_from_private_and_idempotent(self):
        groups.configure(self.root)
        routes, mapping = self.routing()
        self.assertEqual(routes["napcat:FriendMessage:*"], "private")
        self.assertEqual(
            self.private_path.read_bytes(), self.before["data/config/abconf_private.json"]
        )
        profile = groups.read(groups.profile_path(self.root, mapping[routes[groups.GROUP_ROUTE]]))
        self.assertEqual(profile["provider_settings"]["default_provider_id"], "synthetic-chat")
        self.assertEqual(profile["provider_settings"]["default_personality"], "蕾缪安")
        self.assertEqual(profile["provider_settings"]["max_context_length"], 24)
        self.assertEqual(profile["plugin_set"], ["astrbot_plugin_lemuen", "astrbot_plugin_rolebot"])
        self.assertTrue(profile["disable_builtin_commands"])
        self.assertFalse(profile["platform_settings"]["unique_session"])
        self.assertEqual(profile["platform_settings"]["rate_limit"]["count"], 0)
        lemuen = groups.read(self.config / "astrbot_plugin_lemuen_config.json")
        old = json.loads(self.before["data/config/astrbot_plugin_lemuen_config.json"])
        self.assertEqual(lemuen["proactive"], old["proactive"])
        self.assertEqual(lemuen["allowed_sessions"], ["napcat:FriendMessage:*", groups.GROUP_ROUTE])
        rolebot = groups.read(self.config / "astrbot_plugin_rolebot_config.json")
        self.assertTrue(rolebot["groups"]["all_groups"] and rolebot["groups"]["default_enabled"])
        self.assertTrue(rolebot["vision"]["enabled"])
        before_second = self.snapshot()
        groups.configure(self.root)
        self.assertEqual(self.snapshot(), before_second)
        self.assertEqual(self.routing(), (routes, mapping))
        # Future plugin installations merge saved choices over schema defaults.
        schema = groups.read(groups.ROOT / "plugins/astrbot_plugin_rolebot/_conf_schema.json")
        merged = merge(defaults(schema), rolebot)["groups"]
        self.assertEqual({k: merged[k] for k in rolebot["groups"]}, rolebot["groups"])

    def test_unmanaged_existing_route_is_rejected_without_changes(self):
        with sqlite3.connect(self.root / "data/data_v4.db") as db:
            groups.set_preference(
                db,
                "umop_config_routing",
                {"napcat:FriendMessage:*": "private", groups.GROUP_ROUTE: "private"},
            )
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            groups.configure(self.root)
        self.assertEqual(self.snapshot(), self.before)

    def test_apply_restores_files_and_routing_on_failure(self):
        original = groups.configure
        routing = self.routing()

        def fail(root):
            original(root)
            raise RuntimeError("synthetic interruption")

        with (
            patch.object(groups.subprocess, "run") as service,
            patch.object(groups, "configure", fail),
        ):
            service.return_value.returncode = 3
            with (
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "interruption"),
            ):
                groups.apply(self.root)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(self.routing(), routing)
