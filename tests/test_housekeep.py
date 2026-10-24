import argparse
import contextlib
import io
import os
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


class PruneLogs(Base):
    DAY = 86400

    def setUp(self):
        super().setUp()
        self.logs = Path(self.tmp.name) / "logs"
        self.logs.mkdir()
        self.now = 1_000_000_000.0

    def make(self, name, age_s, size=1000):
        path = self.logs / name
        path.write_bytes(b"x" * size)
        os.utime(path, (self.now - age_s, self.now - age_s))
        return path

    def prune(self, **logs):
        d = self.dispatcher()
        if logs:
            d.cfg["logs"] = logs
        return d, d.prune_logs(self.now)

    def test_deletes_old_files_keeps_new_files_and_subfolders_and_reports(self):
        old = self.make("old.jsonl", 20 * self.DAY, 2_000_000)
        new = self.make("new.jsonl", 3 * self.DAY)
        sub = self.logs / "deploy"
        sub.mkdir()
        inner = sub / "inner.log"
        inner.write_text("x")
        os.utime(inner, (self.now - 99 * self.DAY,) * 2)
        os.utime(sub, (self.now - 99 * self.DAY,) * 2)
        _, actions = self.prune()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists() and inner.exists())
        self.assertEqual(len(actions), 1)
        self.assertIn("1 worker log", actions[0])
        self.assertIn("2.0 MB", actions[0])

    def test_keep_days_zero_still_keeps_a_file_from_an_hour_ago(self):
        recent = self.make("running.jsonl", 3600)
        old = self.make("old.jsonl", 2 * self.DAY)
        self.prune(keep_days=0)
        self.assertTrue(recent.exists())
        self.assertFalse(old.exists())

    def test_second_prune_within_a_day_does_not_scan(self):
        d, _ = self.prune()
        old = self.make("late.jsonl", 20 * self.DAY)
        with mock.patch.object(Path, "iterdir", side_effect=AssertionError("scanned")):
            self.assertEqual(d.prune_logs(self.now + self.DAY - 60), [])
        self.assertTrue(old.exists())
        self.assertEqual(len(d.prune_logs(self.now + self.DAY + 60)), 1)
        self.assertFalse(old.exists())

    def test_configured_keep_days_is_honoured(self):
        mid = self.make("mid.jsonl", 20 * self.DAY)
        old = self.make("old.jsonl", 40 * self.DAY)
        self.prune(keep_days=30)
        self.assertTrue(mid.exists())
        self.assertFalse(old.exists())

    def test_default_is_fourteen_days_and_dry_run_deletes_nothing(self):
        self.assertEqual(config.DEFAULTS["logs"]["keep_days"], 14)
        old = self.make("old.jsonl", 20 * self.DAY)
        d = self.dispatcher()
        d.dry_run = True
        d.prune_logs(self.now)
        self.assertTrue(old.exists())
        self.assertIsNone(self.ledger.marked(d.project, "prune-logs"))
