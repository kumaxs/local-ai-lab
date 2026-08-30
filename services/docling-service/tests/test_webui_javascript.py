from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class WebUiJavaScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_concurrent_auth_pagination_and_cursor_state(self) -> None:
        test_file = Path(__file__).with_name("webui_state.test.mjs")
        completed = subprocess.run(
            [shutil.which("node") or "node", "--test", str(test_file)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
