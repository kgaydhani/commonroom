"""commonroom#498: a `run --until-idle` dispatcher hands its slot to a fresh process when the room's code moves."""
import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli
from commonroom.ledger import Ledger

REAL_HEAD = cli.room_head


class FakeDispatcher:
    jobs = []

    def __init__(self, *a, **k):
        pass

    def run_once(self):
        return {"job": FakeDispatcher.jobs.pop(0) if FakeDispatcher.jobs else None}


class Handover(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.heads, self.started = [], []
        FakeDispatcher.jobs = []
        patches = [
            mock.patch.object(cli.config_mod, "load", return_value={"_root": self.tmp.name, "project": {"name": "demo"}}),
            mock.patch.object(cli, "Ledger", lambda: Ledger(home / "room.sqlite3")),
            mock.patch.object(cli, "Dispatcher", FakeDispatcher),
            mock.patch.object(cli, "room_head", lambda: self.heads.pop(0) if self.heads else "a" * 40),
            mock.patch.object(cli, "start_replacement", lambda argv, cwd: self.started.append((argv, cwd))),
            mock.patch.object(cli.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_args(self, **kw):
        base = dict(loop=False, until_idle=True, provider="claude", interval=1, dry_run=False, slot=1)
        return cli.cmd_run(argparse.Namespace(**{**base, **kw}))

    def test_unchanged_head_keeps_going_without_replacement(self):
        FakeDispatcher.jobs = [{"n": 1}, {"n": 2}]
        self.heads = ["a" * 40, "a" * 40, "a" * 40]
        self.assertEqual(self.run_args(), 0)
        self.assertEqual(FakeDispatcher.jobs, [])  # both jobs ran, then the idle pass ended it
        self.assertEqual(self.started, [])

    def test_moved_head_starts_one_replacement_with_same_argv_and_cwd(self):
        FakeDispatcher.jobs = [{"n": 1}, {"n": 2}]
        self.heads = ["a" * 40, "b" * 40]
        with mock.patch.object(sys, "argv", ["room.py", "run", "--until-idle", "--provider", "claude"]):
            self.assertEqual(self.run_args(), 0)
        self.assertEqual(self.started, [([sys.executable, "room.py", "run", "--until-idle", "--provider", "claude"], os.getcwd())])
        self.assertEqual(FakeDispatcher.jobs, [{"n": 2}])  # it returned before the second job
        self.assertFalse((Path(self.tmp.name) / "demo.lock").exists())

    def test_idle_pass_never_replaces(self):
        self.heads = ["a" * 40, "b" * 40]
        self.assertEqual(self.run_args(), 0)
        self.assertEqual(self.started, [])

    def test_loop_and_dry_run_never_replace(self):
        for kw in ({"loop": True, "until_idle": False}, {"dry_run": True}):
            FakeDispatcher.jobs = [{"n": 1}]
            self.heads = ["a" * 40, "b" * 40]
            with mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
                self.run_args(**kw)
            self.assertEqual(self.started, [], kw)

    def test_unreadable_head_counts_as_unchanged(self):
        FakeDispatcher.jobs = [{"n": 1}]
        self.heads = ["a" * 40, None]
        self.assertEqual(self.run_args(), 0)
        self.assertEqual(self.started, [])
        with mock.patch.object(cli.subprocess, "run", side_effect=OSError("no git")):
            self.assertIsNone(REAL_HEAD())
        bad = mock.Mock(returncode=128, stdout="")
        with mock.patch.object(cli.subprocess, "run", return_value=bad):
            self.assertIsNone(REAL_HEAD())


if __name__ == "__main__":
    unittest.main()


class ReplacementStreams(unittest.TestCase):
    def test_child_gets_this_process_output_streams(self):
        with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
            with mock.patch.object(cli.sys, "stdout", out), mock.patch.object(cli.sys, "stderr", err), \
                    mock.patch.object(cli.subprocess, "Popen") as popen:
                cli.start_replacement(["x"], "somewhere")
            popen.assert_called_once_with(["x"], cwd="somewhere", stdout=out, stderr=err)

    def test_real_child_writes_to_the_same_file(self):
        with tempfile.TemporaryFile("w+b") as out:
            class S:
                def flush(self): pass
                def fileno(self): return out.fileno()
            with mock.patch.object(cli.sys, "stdout", S()), mock.patch.object(cli.sys, "stderr", S()):
                p = cli.start_replacement([sys.executable, "-c", "import sys;print('o');print('e',file=sys.stderr)"], os.getcwd())
            p.wait()
            out.seek(0)
            self.assertEqual(sorted(out.read().split()), [b"e", b"o"])

    def test_no_stream_handle_falls_back(self):
        with mock.patch.object(cli.sys, "stdout", None), mock.patch.object(cli.sys, "stderr", None), \
                mock.patch.object(cli.subprocess, "Popen") as popen:
            cli.start_replacement(["x"], "d")
        popen.assert_called_once_with(["x"], cwd="d")
