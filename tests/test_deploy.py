"""Exercise deployment commit/rollback without touching a service or real data."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.deploy_release import Deployment


class FakeDeployment(Deployment):
    def __init__(self, source, target, fail=False):
        super().__init__(source, target, "a" * 40, sys.executable)
        self.actions = []
        self.fail = fail

    def service(self, action):
        self.actions.append(action)

    def run(self, args, *, cwd=None):
        if "install" in args and "scripts/manage_plugins.py" in args:
            cfg = self.target / "data/config/plugin.json"
            cfg.write_text('{"new": true}')
            (self.target / "data/plugins/new-plugin").mkdir()
            (self.target / "data/plugins/new-plugin/main.py").write_text("NEW PLUGIN")
            (self.target / "data/data_v4.db").write_bytes(b"changed sqlite")
            if self.fail:
                raise RuntimeError("synthetic failed install")

    def health(self):
        pass


class DeploymentTests(unittest.TestCase):
    def setup_dirs(self, folder):
        root = Path(folder)
        source, target = root / "candidate", root / "live"
        source.mkdir()
        target.mkdir()
        (source / "app.py").write_text("new code")
        (target / "app.py").write_text("old code")
        (source / "new.py").write_text("newly added code")
        (target / "removed.py").write_text("obsolete tracked code")
        (target / "personal-notes.md").write_text("keep user notes")
        for path in (
            "data/config",
            "data/plugins",
            "data/plugin_data",
            "runtime/plugins",
            "runtime/backups",
            "runtime/deploy",
        ):
            (target / path).mkdir(parents=True, exist_ok=True)
        (target / "data/config/plugin.json").write_text('{"user_setting": true}')
        (target / "data/cmd_config.json").write_text('{"saved": true}')
        (target / "data/data_v4.db").write_bytes(b"old sqlite")
        (target / "data/data_v4.db-wal").write_bytes(b"old wal")
        (target / "runtime/deployed-files.json").write_text('["app.py", "removed.py"]')
        (target / "runtime/deployed-revision").write_text("b" * 40 + "\n")
        (target / "runtime/plugins/activation.json").write_text('{"old": true}')
        for folder_name in ("runtime/lemuen/build", "runtime/plugins/build"):
            (source / folder_name).mkdir(parents=True, exist_ok=True)
            (source / folder_name / "plugin.zip").write_bytes(b"new zip")
            (target / folder_name).mkdir(parents=True, exist_ok=True)
            (target / folder_name / "plugin.zip").write_bytes(b"old zip")
        return source, target

    def test_success_installs_source_and_keeps_unknown_user_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = self.setup_dirs(tmp)
            deploy = FakeDeployment(source, target)
            with patch(
                "scripts.deploy_release.subprocess.check_output", return_value="package==1\n"
            ):
                deploy.activate()
            self.assertEqual((target / "app.py").read_text(), "new code")
            self.assertFalse((target / "removed.py").exists())
            self.assertEqual((target / "personal-notes.md").read_text(), "keep user notes")
            self.assertEqual(deploy.actions, ["stop", "start"])
            self.assertEqual((target / "runtime/deployed-revision").read_text().strip(), "a" * 40)
            self.assertTrue((deploy.backup / "data/config/plugin.json").is_file())

    def test_failed_install_restores_plugins_sqlite_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = self.setup_dirs(tmp)
            deploy = FakeDeployment(source, target, fail=True)
            with patch(
                "scripts.deploy_release.subprocess.check_output", return_value="package==1\n"
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic failed install"):
                    deploy.activate()
            self.assertEqual((target / "app.py").read_text(), "old code")
            self.assertFalse((target / "new.py").exists())
            self.assertTrue((target / "removed.py").is_file())
            self.assertFalse((target / "data/plugins/new-plugin").exists())
            self.assertEqual(
                json.loads((target / "data/config/plugin.json").read_text()), {"user_setting": True}
            )
            self.assertEqual((target / "data/data_v4.db").read_bytes(), b"old sqlite")
            self.assertEqual((target / "data/data_v4.db-wal").read_bytes(), b"old wal")
            self.assertEqual((target / "runtime/plugins/build/plugin.zip").read_bytes(), b"old zip")
            self.assertEqual((target / "runtime/deployed-revision").read_text().strip(), "b" * 40)
            self.assertEqual(deploy.actions, ["stop", "stop", "start"])

    def test_failed_backup_restarts_service_without_overwriting_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = self.setup_dirs(tmp)
            deploy = FakeDeployment(source, target)
            with patch("scripts.deploy_release.snapshot", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    deploy.activate()
            self.assertEqual((target / "data/data_v4.db").read_bytes(), b"old sqlite")
            self.assertEqual(deploy.actions[-1], "start")

    def test_manifest_cannot_delete_unmanaged_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = self.setup_dirs(tmp)
            (target / "runtime/deployed-files.json").write_text('["../outside"]')
            deploy = FakeDeployment(source, target)
            with self.assertRaisesRegex(ValueError, "Invalid deployment file manifest"):
                deploy.activate()
            self.assertEqual(deploy.actions, [])


class PluginDeploymentConfigTests(unittest.TestCase):
    def test_vision_migration_only_updates_the_managed_default_model(self):
        from scripts.manage_plugins import VISION_MODEL, upgrade_managed_vision

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data/config").mkdir(parents=True)
            managed = {
                "id": "lemuen-vision",
                "provider_source_id": "lemuen-dashscope",
                "model": "qwen3-vl-plus",
                "enable": False,
                "custom_extra_body": {"max_tokens": 3000},
            }
            custom = {**managed, "model": "custom-user-model"}
            other = {**managed, "id": "another-vision"}
            main = root / "data/cmd_config.json"
            main.write_text(json.dumps({"provider": [managed, other], "plugin_set": []}))
            profile = root / "data/config/abconf-private.json"
            profile.write_text(json.dumps({"provider": [custom], "custom": True}))
            before = profile.read_bytes()
            upgrade_managed_vision(root)
            updated = json.loads(main.read_text())
            self.assertEqual(updated["provider"][0], {**managed, "model": VISION_MODEL})
            self.assertEqual(updated["provider"][1], other)
            self.assertEqual(updated["plugin_set"], [])
            self.assertEqual(profile.read_bytes(), before)

    def test_reinstall_preserves_disabled_plugins_providers_and_preferences(self):
        import zipfile
        from unittest.mock import Mock

        from scripts import manage_plugins

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / "runtime/plugins/build"
            build.mkdir(parents=True)
            own_build = root / "runtime/lemuen/build"
            own_build.mkdir(parents=True)
            (root / "data/config").mkdir(parents=True)
            (root / "data/plugins").mkdir()
            (root / "data/cmd_config.json").write_text(
                '{"plugin_set": [], "provider": [], "user_setting": true}'
            )
            main_before = (root / "data/cmd_config.json").read_bytes()
            (root / "runtime/plugins/activation.json").write_text(
                '{"profile_revision": 2, "proactive_recipients": 1}'
            )
            cfg_path = root / "data/config/astrbot_plugin_rolebot_config.json"
            cfg_path.write_text(
                '{"enabled": false, "search": {"enabled": false}, "custom": "keep"}'
            )
            lock = root / "plugins/lock.json"
            lock.parent.mkdir()
            lock.write_text('{"upstream": [], "memory_prompts": {}}')
            with zipfile.ZipFile(own_build / "astrbot_plugin_lemuen.zip", "w") as archive:
                archive.writestr("astrbot_plugin_lemuen/main.py", "# new version")
            with zipfile.ZipFile(build / "astrbot_plugin_rolebot.zip", "w") as archive:
                archive.writestr("astrbot_plugin_rolebot/main.py", "# new version")
                archive.writestr(
                    "astrbot_plugin_rolebot/_conf_schema.json",
                    json.dumps(
                        {
                            "enabled": {"type": "bool", "default": True},
                            "search": {
                                "type": "object",
                                "items": {"enabled": {"type": "bool", "default": True}},
                            },
                        }
                    ),
                )
            backup = root / "runtime/backups/prepared"
            (backup / "data").mkdir(parents=True)
            (backup / "data/cmd_config.json").write_bytes(main_before)
            with (
                patch.multiple(manage_plugins, ROOT=root, BUILD=build, LOCK=lock),
                patch.object(manage_plugins.subprocess, "run", return_value=Mock(returncode=3)),
            ):
                manage_plugins.install(backup_dir=backup, skip_dependencies=True)
            self.assertEqual((root / "data/cmd_config.json").read_bytes(), main_before)
            config = json.loads(cfg_path.read_text())
            self.assertFalse(config["enabled"])
            self.assertFalse(config["search"]["enabled"])
            self.assertEqual(config["custom"], "keep")
            self.assertTrue((root / "data/plugins/astrbot_plugin_rolebot/main.py").is_file())


class RunnerGateTests(unittest.TestCase):
    def test_runner_accepts_only_main_deploy_workflow(self):
        import os
        import subprocess

        from scripts.manage_runner import JOB_GATE

        good = dict(
            os.environ,
            GITHUB_REPOSITORY="DuscWalk/Chatbot",
            GITHUB_REF="refs/heads/main",
            GITHUB_WORKFLOW_REF="DuscWalk/Chatbot/.github/workflows/ci-cd.yml@refs/heads/main",
            GITHUB_EVENT_NAME="push",
        )
        for patch_env, expected in [
            ({}, 0),
            ({"GITHUB_EVENT_NAME": "workflow_dispatch"}, 0),
            ({"GITHUB_EVENT_NAME": "pull_request"}, 1),
            ({"GITHUB_REPOSITORY": "other/Chatbot"}, 1),
            ({"GITHUB_REF": "refs/pull/12/merge"}, 1),
            (
                {
                    "GITHUB_WORKFLOW_REF": "DuscWalk/Chatbot/.github/workflows/evil.yml@refs/heads/main"
                },
                1,
            ),
        ]:
            result = subprocess.run(
                ["bash", "-c", JOB_GATE], env=good | patch_env, capture_output=True
            )
            self.assertEqual(result.returncode, expected, patch_env)
