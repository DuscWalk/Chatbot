"""Exercise the real AstrBot initializer in isolated project directories."""

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import manage


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbots2-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / "runtime").mkdir()
        shutil.copyfile(manage.ROOT / "requirements.txt", self.root / "requirements.txt")
        self.patch_root(self.root)
        # Keep CLI output out of test logs (the subprocess is still real).
        self.actual_run = manage.run
        self.runner = mock.patch.object(manage, "run", self.quiet_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def patch_root(self, root):
        patch = mock.patch.multiple(
            manage,
            ROOT=root,
            DATA=root / "data",
            RUNTIME=root / "runtime",
            NAPCAT=root / "runtime/napcat/config",
        )
        patch.start()
        self.addCleanup(patch.stop)

    def quiet_run(self, args, **kwargs):
        return self.actual_run(args, capture_output=True, text=True, **kwargs)

    def initialize(self):
        with contextlib.redirect_stdout(io.StringIO()):
            manage.setup()

    def test_initial_credentials_and_loopback_connection(self):
        self.initialize()
        config = manage.read_json(manage.DATA / "cmd_config.json")
        credentials = manage.read_json(manage.RUNTIME / "credentials.json")
        webui = manage.read_json(manage.NAPCAT / "webui.json")
        client = manage.read_json(manage.NAPCAT / "onebot11.json")["network"]["websocketClients"][0]
        platform = config["platform"][0]
        self.assertEqual(config["dashboard"]["host"], "127.0.0.1")
        self.assertEqual(platform["ws_reverse_host"], "127.0.0.1")
        self.assertEqual(webui["host"], "127.0.0.1")
        self.assertEqual(client["url"], "ws://127.0.0.1:6199/ws")
        self.assertEqual(client["token"], platform["ws_reverse_token"])
        self.assertEqual(client["token"], credentials["onebot_token"])
        self.assertGreaterEqual(len(client["token"]), 32)
        self.assertNotEqual(webui["token"], client["token"])
        self.assertEqual((manage.RUNTIME / "credentials.json").stat().st_mode & 0o777, 0o600)
        algorithm, count, salt, stored = config["dashboard"]["pbkdf2_password"].split("$")
        self.assertEqual(algorithm, "pbkdf2_sha256")
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            credentials["astrbot_initial_password"].encode(),
            bytes.fromhex(salt),
            int(count),
        ).hex()
        self.assertEqual(actual, stored)
        self.assertTrue((manage.NAPCAT / "napcat.json").exists())

    def test_repeat_setup_preserves_webui_settings_and_bom(self):
        self.initialize()
        config_path = manage.DATA / "cmd_config.json"
        config = manage.read_json(config_path)
        config["provider_settings"]["default_provider_id"] = "user-selected-model"
        config["dashboard"]["username"] = "custom-user"
        # AstrBot itself saves UTF-8 with a BOM.
        config_path.write_text(json.dumps(config), encoding="utf-8-sig")
        account = manage.NAPCAT / "onebot11_123456789.json"
        account.write_text('{"user_setting": true}')
        protected = [
            config_path,
            account,
            manage.RUNTIME / "credentials.json",
            manage.NAPCAT / "webui.json",
        ]
        before = {p: p.read_bytes() for p in protected}
        self.initialize()
        self.assertEqual(before, {p: p.read_bytes() for p in protected})

    def test_interrupted_initialization_can_resume(self):
        def interrupt_after_astrbot_init(args, **kwargs):
            self.quiet_run(args, **kwargs)
            raise RuntimeError("simulated interruption after upstream init")

        with mock.patch.object(manage, "run", interrupt_after_astrbot_init):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                self.initialize()
        self.assertTrue((manage.RUNTIME / "setup.pending").exists())
        saved = (manage.RUNTIME / "credentials.json").read_bytes()
        self.initialize()
        self.assertFalse((manage.RUNTIME / "setup.pending").exists())
        self.assertEqual(saved, (manage.RUNTIME / "credentials.json").read_bytes())
        config = manage.read_json(manage.DATA / "cmd_config.json")
        self.assertEqual(config["platform"][0]["id"], "napcat")
        self.assertEqual(config["dashboard"]["host"], "127.0.0.1")

    def test_migration_updates_only_our_media_mapping(self):
        self.initialize()
        config_file = manage.DATA / "cmd_config.json"
        config = manage.read_json(config_file)
        config["platform_settings"]["path_mapping"].append("/custom/source:/custom/destination")
        config["provider_settings"]["default_provider_id"] = "saved-provider"
        manage.write_json(config_file, config)
        credentials = (manage.RUNTIME / "credentials.json").read_bytes()
        migrated = self.root.parent / "server-project"
        shutil.copytree(self.root, migrated)
        self.patch_root(migrated)
        self.initialize()
        config = manage.read_json(manage.DATA / "cmd_config.json")
        self.assertEqual(
            config["platform_settings"]["path_mapping"],
            [
                f"/app/.config/QQ:{migrated}/runtime/napcat/qq",
                "/custom/source:/custom/destination",
            ],
        )
        self.assertEqual(config["provider_settings"]["default_provider_id"], "saved-provider")
        self.assertEqual(credentials, (manage.RUNTIME / "credentials.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
