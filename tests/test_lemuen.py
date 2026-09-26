"""Run plugin checks in a disposable AstrBot root; never touch local bot data."""

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.lemuen_corpus import import_payload, read_bundle, validate


class LemuenTests(unittest.TestCase):
    def test_user_lyrics_import_as_one_complete_source_marked_chunk(self):
        bundle = read_bundle()
        entries, sources = validate(bundle, None)
        lyrics = entries["L108"]["summary"]
        documents = import_payload(bundle, entries, sources)["documents"]
        song = next(d for d in documents if d["file_name"] == "蕾缪安-角色EP与歌曲.md")
        self.assertEqual(len(song["chunks"]), 1)
        self.assertIn(lyrics, song["chunks"][0])
        self.assertEqual(len(lyrics.splitlines()), 52)
        self.assertIn("用户提供", song["chunks"][0])
        altered = copy.deepcopy(bundle)
        next(e for e in altered["entries"]["entries"] if e["id"] == "L108")["summary"] += (
            "\nExtra line"
        )
        with self.assertRaisesRegex(ValueError, "用户原文不一致"):
            validate(altered, None)
        wrong_source = copy.deepcopy(bundle)
        wrong_source["entries"]["entries"][0]["evidence"] = [
            {"source_id": "UEP1", "locator": "L1-L52"}
        ]
        with self.assertRaisesRegex(ValueError, "不能作为原作事实依据"):
            validate(wrong_source, None)

    def test_plugin_boundaries_in_isolated_astrbot(self):
        with tempfile.TemporaryDirectory(prefix="lemuen-test-") as root:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("plugin_scenarios.py"))],
                env={**os.environ, "ASTRBOT_ROOT": root},
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
