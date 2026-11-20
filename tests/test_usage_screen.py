"""The hidden-terminal /usage reader says when the Claude CLI is logged out (commonroom#236)."""
import os
import sys
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from commonroom import usage

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Captured on Claude Code 2.1.322 with a logged-out CLI; the folder path is replaced, no account details shown.
LOGGED_OUT = (FIXTURES / "claude_logged_out_screen.txt").read_text(encoding="utf-8")
TZ = "Europe/London"
PANEL = f"""
 Claude Code v2.1.322
 Current session
 ███ 9% used
 Resets 1pm ({TZ})
 Current week (all models)
 █ 27% used
 Resets Oct 8, 2am ({TZ})
 Current week (Fable)
 ████ 47% used
 Resets Oct 8, 2am ({TZ})
 Extra usage not enabled
"""


def fake_terminal(screen_text: str):
    """Stand-ins for pywinpty and pyte: the terminal shows `screen_text` as soon as it starts."""
    class Process:
        terminated = False

        def __init__(self):
            self.sent = [screen_text]

        def isalive(self):
            return bool(self.sent)

        def read(self, _n):
            if self.sent:
                return self.sent.pop()
            raise EOFError

        def write(self, _text):
            pass

        def terminate(self, force=False):
            Process.terminated = True

    class Screen:
        def __init__(self, *_):
            self.display = []

    class Stream:
        def __init__(self, screen):
            self.screen = screen

        def feed(self, chunk):
            self.screen.display = chunk.splitlines()

    winpty = types.SimpleNamespace(PtyProcess=types.SimpleNamespace(spawn=lambda *a, **k: Process()))
    return {"winpty": winpty, "pyte": types.SimpleNamespace(Screen=Screen, Stream=Stream)}, Process


class LoggedOutScreen(unittest.TestCase):
    def test_logged_out_screen_is_named(self):
        reason = usage.claude_screen_blocker(LOGGED_OUT, "C:/work/project")
        self.assertIn("logged out", reason)
        self.assertIn("/login", reason)

    def test_normal_panel_still_parses(self):
        self.assertIsNone(usage.claude_screen_blocker(PANEL, "C:/work/project"))
        now = datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo(TZ)).timestamp()
        obs = {o["window"]: o["used_pct"] for o in usage.parse_claude_screen(PANEL, now)}
        self.assertEqual(obs, {"five_hour": 9, "seven_day": 27, "seven_day_fable": 47})

    def test_trust_prompt_message_unchanged(self):
        reason = usage.claude_screen_blocker("Do you trust the files?\n❯ 1. Yes, I trust this folder", "C:/x")
        self.assertEqual(reason, "Open Claude once in C:/x and trust the folder to enable the /usage reader")

    @unittest.skipUnless(os.name == "nt", "the screen reader is Windows only")
    def test_reader_stops_at_once_when_logged_out(self):
        modules, process = fake_terminal(LOGGED_OUT)
        with mock.patch.dict(sys.modules, modules), mock.patch.object(usage, "claude_executable", return_value="claude"):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "logged out"):
                usage.claude_screen_limits(cwd="C:/work/project", timeout=30)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
