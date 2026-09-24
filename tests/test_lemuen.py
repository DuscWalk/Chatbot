"""Run plugin checks in a disposable AstrBot root; never touch local bot data."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LemuenTests(unittest.TestCase):
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
