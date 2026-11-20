"""One cooldown per provider for the meter reader, shared by every process (commonroom#235)."""
import argparse
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, config, dispatch, usage
from commonroom.dispatch import READER_COOLDOWN
from commonroom.ledger import Ledger


def cfg(root):
    c = config.merge(config.DEFAULTS, {"project": {"name": "t", "repo": "o/t"}, "goal": {"milestone": "M0"}})
    c["_root"] = str(root)
    return c


def reading(provider, pct=5):
    return [usage.observation(provider, "five_hour", pct, None, "t")]


class ReaderCooldown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "room.sqlite3"
        self.ledgers = []
        self.claude = mock.Mock(side_effect=RuntimeError("Claude /usage panel did not render a complete reading"))
        self.codex = mock.Mock(side_effect=lambda: reading("codex"))
        for target, m in (("claude_screen_limits", self.claude), ("codex_limits", self.codex)):
            p = mock.patch.object(usage, target, m)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for ledger in self.ledgers:
            ledger.close()
        self.tmp.cleanup()

    def connect(self):
        ledger = Ledger(self.path, synchronous="OFF")
        self.ledgers.append(ledger)
        return ledger

    def process(self):
        """A dispatcher with its own ledger connection to the shared file, as another process would have."""
        return dispatch.Dispatcher(cfg(self.tmp.name), self.connect(), board=mock.Mock(), log=lambda *_: None)

    def probe(self, provider="claude"):
        ledger = self.connect()
        with mock.patch.object(cli, "Ledger", return_value=ledger), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_probe(argparse.Namespace(provider=provider))
        return out.getvalue()

    def test_two_processes_launch_the_failing_reader_once(self):
        a, b = self.process(), self.process()
        now = time.time()
        a.refresh_readings(now)
        b.refresh_readings(now + 5)
        a.refresh_readings(now + 600)
        self.assertEqual(self.claude.call_count, 1)

    def test_it_tries_again_after_the_cooldown(self):
        a, b = self.process(), self.process()
        now = time.time()
        a.refresh_readings(now)
        b.refresh_readings(now + READER_COOLDOWN + 1)
        self.assertEqual(self.claude.call_count, 2)

    def test_a_success_resets_it(self):
        a, b = self.process(), self.process()
        now = time.time()
        a.refresh_readings(now)
        self.claude.side_effect = None
        self.claude.return_value = reading("claude")
        b.refresh_readings(now + READER_COOLDOWN + 1)
        self.assertEqual(self.claude.call_count, 2)
        self.assertIsNone(a.ledger.marked(None, "reader_cooldown:claude"))
        self.claude.side_effect = RuntimeError("again")
        a.refresh_readings(now + 4 * 3600)   # stale again, and nothing holds the reader back
        self.assertEqual(self.claude.call_count, 3)

    def test_fresh_readings_launch_nothing(self):
        a = self.process()
        a.ledger.record_usage(reading("claude"))
        a.ledger.record_usage(reading("codex"))
        a.refresh_readings(time.time())
        self.claude.assert_not_called()
        self.codex.assert_not_called()
        self.assertIsNone(a.ledger.marked(None, "reader_cooldown:claude"))

    def test_room_probe_shares_the_cooldown(self):
        self.process().refresh_readings(time.time())   # a dispatcher tried and failed
        out = self.probe()
        self.assertEqual(self.claude.call_count, 1)    # the probe did not launch the reader again
        self.assertIn("skipped", out)

    def test_room_probe_failing_holds_the_dispatchers_off(self):
        self.probe()
        self.assertEqual(self.claude.call_count, 1)
        self.process().refresh_readings(time.time())
        self.assertEqual(self.claude.call_count, 1)

    def test_room_probe_success_clears_it(self):
        self.process().refresh_readings(time.time() - READER_COOLDOWN - 10)
        self.claude.side_effect = None
        self.claude.return_value = reading("claude")
        self.probe()
        self.assertEqual(self.claude.call_count, 2)
        self.assertIsNone(self.connect().marked(None, "reader_cooldown:claude"))


if __name__ == "__main__":
    unittest.main()
