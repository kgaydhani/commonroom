import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, config, dispatch, gate, lock
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with, issue

LIVE = 4242


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.ledger.close)
        self.lock_path = Path(self.tmp.name) / "t-housekeep.lock"

    def dispatcher(self):
        board = FakeBoard([issue(4, ["status:ready", "role:builder"])])
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None)
        d.refresh_readings = lambda now: None
        d.allowed = lambda now: {"claude": gate.Verdict(True, "ok")}
        d.run_job = mock.Mock(return_value={})
        d.pick = mock.Mock(wraps=d.pick)
        d.calls = []
        d.housekeeping = lambda: d.calls.append(1) or []
        return d

    def hold(self):
        self.lock_path.write_text(str(LIVE))
        return mock.patch.object(lock, "alive", lambda pid: pid == LIVE)


class HousekeepLoop(Base):
    def test_runs_at_the_interval_until_the_minutes_are_up_and_never_picks(self):
        d = self.dispatcher()
        now = [0.0]
        sleeps = []
        def sleep(s):
            sleeps.append(s)
            now[0] += s
        passes = cli.housekeep_loop(d, 14, 180, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(passes, len(d.calls))
        self.assertEqual(sleeps, [180.0] * len(sleeps))
        self.assertEqual(passes, 5)           # at 0, 180, 360, 540, 720 s; 900 s is past 14 minutes (840 s)
        d.pick.assert_not_called()
        d.run_job.assert_not_called()
        self.assertFalse(self.lock_path.exists())

    def test_exits_zero_without_housekeeping_when_another_live_pid_holds_the_lock(self):
        args = argparse.Namespace(minutes=14, every=180)
        cfg = cfg_with(self.tmp.name)
        with self.hold(), mock.patch.object(config, "load", lambda: cfg), \
                mock.patch.object(cli, "Ledger", lambda: self.ledger), \
                mock.patch.object(self.ledger, "close"), \
                mock.patch.object(dispatch.Dispatcher, "housekeeping") as hk, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_housekeep(args), 0)
        hk.assert_not_called()
        self.assertEqual(self.lock_path.read_text(), str(LIVE))


class RunOnceLock(Base):
    def test_a_held_lock_skips_housekeeping_but_still_runs_a_job(self):
        d = self.dispatcher()
        with self.hold():
            report = d.run_once()
        self.assertEqual(d.calls, [])
        self.assertEqual(report["housekeeping"], [])
        self.assertEqual(report["job"]["kind"], "build")
        self.assertEqual(self.lock_path.read_text(), str(LIVE))

    def test_no_lock_held_runs_housekeeping_and_releases_it(self):
        d = self.dispatcher()
        report = d.run_once()
        self.assertEqual(d.calls, [1])
        self.assertEqual(report["job"]["kind"], "build")
        self.assertFalse(self.lock_path.exists())

    def test_a_lock_left_by_a_dead_process_is_taken_over(self):
        d = self.dispatcher()
        self.lock_path.write_text("999999")
        with mock.patch.object(lock, "alive", lambda pid: False):
            d.run_once()
        self.assertEqual(d.calls, [1])


class Heartbeat(unittest.TestCase):
    def test_starts_housekeep_for_both_projects_inside_the_block(self):
        text = (Path(__file__).resolve().parent.parent / "heartbeat.cmd").read_text()
        lines = text.splitlines()
        start, end = lines.index("("), lines.index(")")
        block = [l for l in lines[start:end] if "housekeep" in l]
        self.assertEqual(len(block), 2)
        self.assertTrue(all("room.py\" housekeep" in l for l in block))
        self.assertTrue(any(l.lstrip().startswith('start "" /b cmd /c') for l in block))
        self.assertTrue(any('if exist "%~dp0room.toml"' in l and '/d "%~dp0."' in l for l in block))
        self.assertFalse(any(l.lstrip().startswith("rem") for l in lines[start:end]))


if __name__ == "__main__":
    unittest.main()
