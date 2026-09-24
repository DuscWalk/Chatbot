import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.astrbot_plugin_lemuen.proactive import ZONE, ProactiveChat

MODULE = "plugins.astrbot_plugin_lemuen.proactive"
TARGET = "napcat:FriendMessage:synthetic-doctor"
NOW = datetime(2026, 9, 26, 20, 0, tzinfo=ZONE).timestamp()


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(NOW, tz)


class ProactiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        plugin = SimpleNamespace(
            config={
                "enabled": True,
                "allowed_sessions": ["napcat:FriendMessage:*"],
                "proactive": {"enabled": True, "targets": [TARGET]},
            }
        )
        self.scheduler = ProactiveChat(plugin, Path(self.folder.name) / "state.json")
        self.scheduler.boot_time = NOW - 10800
        self.scheduler.state[TARGET] = {
            "last_activity": NOW - 10800,
            "schedule_day": "2026-09-26",
            "due": NOW - 60,
        }

    def test_private_scope_quiet_hours_and_recent_activity(self):
        s = self.scheduler
        self.assertTrue(s.eligible(TARGET, NOW))
        self.assertFalse(s.eligible("napcat:FriendMessage:someone-else", NOW))
        self.assertFalse(s.eligible("napcat:GroupMessage:synthetic", NOW))
        self.assertFalse(s.eligible(TARGET, NOW + 7200))
        self.assertFalse(s.eligible(TARGET, NOW + 86400))
        s.state[TARGET]["last_activity"] = NOW - 60
        self.assertFalse(s.eligible(TARGET, NOW))

    async def test_one_attempt_survives_restart_and_failure(self):
        s = self.scheduler
        s.generate = AsyncMock(side_effect=TimeoutError)
        with (
            patch(MODULE + ".time.time", return_value=NOW),
            patch(MODULE + ".datetime", FrozenDatetime),
        ):
            with self.assertRaises(TimeoutError):
                await s.tick(TARGET)
            await s.tick(TARGET)
        self.assertEqual(s.generate.await_count, 1)
        restarted = ProactiveChat(s.plugin, s.path)
        restarted.boot_time = NOW - 10800
        self.assertFalse(restarted.eligible(TARGET, NOW))
        self.assertEqual(s.path.stat().st_mode & 0o777, 0o600)

    def test_new_incoming_message_cancels_draft(self):
        s = self.scheduler
        draft = {"activity": s.state[TARGET]["last_activity"]}
        with (
            patch(MODULE + ".time.time", return_value=NOW),
            patch(MODULE + ".datetime", FrozenDatetime),
        ):
            self.assertTrue(s.still_idle(TARGET, draft))
            s.activity(TARGET)
            self.assertFalse(s.still_idle(TARGET, draft))
