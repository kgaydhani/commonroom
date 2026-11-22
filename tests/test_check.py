"""commonroom#180: `room.py check` runs the project's test command under a per-project lock, so parallel workers queue."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, config, dispatch
from commonroom.ledger import Ledger
from commonroom.lock import alive, claim_lock, release_lock

REPO = Path(__file__).resolve().parent.parent
ROOM = REPO / "room.py"


class CheckCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.lock = self.home / "locks" / "demo-suite.lock"
        self.env = {**os.environ, "COMMONROOM_HOME": str(self.home)}

    def tearDown(self):
        self.tmp.cleanup()

    def project_with(self, test, extra=""):
        script = self.project / "suite.py"
        script.write_text("import sys, time\n"
                          "log = open(sys.argv[1], 'a')\n"
                          "log.write(f'start {time.time()}\\n'); log.flush()\n"
                          "time.sleep(float(sys.argv[2]))\n"
                          "log.write(f'end {time.time()}\\n'); log.close()\n"
                          "sys.exit(int(sys.argv[3]))\n")
        quoted = f'"{sys.executable}" "{script}" ' + test
        (self.project / "room.toml").write_text(
            f"[project]\nname = \"demo\"\nrepo = \"o/demo\"\n[goal]\nmilestone = \"M\"\n[checks]\ntest = '{quoted}'\n{extra}")

    def start(self):
        return subprocess.Popen([sys.executable, str(ROOM), "check"], cwd=self.project, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def run_check(self):
        p = self.start()
        out, _ = p.communicate(timeout=60)
        return p.returncode, out

    @contextlib.contextmanager
    def local_check(self):
        cfg = config.load(self.project / "room.toml")
        with mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)}), \
                mock.patch.object(cli.config_mod, "load", return_value=cfg), \
                mock.patch("os.getcwd", return_value=str(self.project)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            yield out

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
        return p.pid

    def test_unwritable_room_home_runs_unlocked(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        self.env["COMMONROOM_HOME"] = str(blocker / "sub")
        log = self.project / "log.txt"
        self.project_with(f"\"{log}\" 0 7")
        code, out = self.run_check()
        self.assertEqual(code, 7)
        self.assertIn("running unlocked", out)

    def test_lock_unwritable_after_waiting_runs_unlocked(self):
        # Codex: the first claim reads a live holder, the retry after it lets go can't create the file.
        log = self.project / "log.txt"
        self.project_with(f"\"{log}\" 0 7", "lock_poll_seconds = 0.05\n")
        cfg = config.load(self.project / "room.toml")
        claims = iter([os.getpid() + 1, PermissionError("sandbox")])

        def claim(path):
            got = next(claims)
            if isinstance(got, Exception):
                raise got
            return got
        with mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)}),                 mock.patch.object(cli.config_mod, "load", return_value=cfg),                 mock.patch.object(cli, "claim_lock", side_effect=claim),                 mock.patch("os.getcwd", return_value=str(self.project)),                 contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.cmd_check(None)
        self.assertEqual(code, 7)
        self.assertIn("waiting for the suite lock", out.getvalue())
        self.assertIn("running unlocked", out.getvalue())
        self.assertIn("start", log.read_text())

    def test_alive_matches_the_pid_column_only(self):
        from commonroom.lock import alive
        self.assertTrue(alive(os.getpid()))
        self.assertFalse(alive(self.dead_pid()))

    def test_two_runs_take_turns(self):
        log = self.project / "log.txt"
        self.project_with(f"\"{log}\" 1.5 0", "lock_poll_seconds = 0.2\n")
        a, b = self.start(), self.start()
        outs = [a.communicate(timeout=60)[0], b.communicate(timeout=60)[0]]
        self.assertEqual((a.returncode, b.returncode), (0, 0))
        marks = [line.split() for line in log.read_text().splitlines()]
        self.assertEqual([m[0] for m in marks], ["start", "end", "start", "end"])   # never overlapping
        times = [float(m[1]) for m in marks]
        self.assertEqual(times, sorted(times))
        self.assertTrue(any("waiting for the suite lock (pid " in o for o in outs))
        self.assertFalse(self.lock.exists())

    def test_exit_code_passes_through(self):
        self.project_with(f"\"{self.project / 'log.txt'}\" 0 7")
        self.assertEqual(self.run_check()[0], 7)
        self.assertFalse(self.lock.exists())

    def test_dead_holder_is_taken_over_at_once(self):
        self.project_with(f"\"{self.project / 'log.txt'}\" 0 0")
        self.lock.parent.mkdir(parents=True)
        self.lock.write_text(str(self.dead_pid()))
        started = time.time()
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertNotIn("waiting", out)
        self.assertLess(time.time() - started, 30)

    def test_wait_runs_out_with_exit_3(self):
        self.project_with(f"\"{self.project / 'log.txt'}\" 0 0", "lock_wait_minutes = 0.02\nlock_poll_seconds = 0.2\n")
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.lock.parent.mkdir(parents=True)
            self.lock.write_text(str(holder.pid))
            code, out = self.run_check()
        finally:
            holder.kill(); holder.wait()
        self.assertEqual(code, 3)
        self.assertIn(f"waiting for the suite lock (pid {holder.pid})", out)
        self.assertIn("suite lock busy: report TESTS: INCOMPLETE", out)
        self.assertFalse((self.project / "log.txt").exists())          # the suite never ran

    def test_suite_past_max_minutes_is_killed(self):
        child = self.project / "child.pid"
        script = self.project / "slow.py"
        script.write_text("import subprocess, sys, time\n"
                          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                          f"open(r'{child}', 'w').write(str(c.pid))\n"
                          "time.sleep(60)\n")
        (self.project / "room.toml").write_text(
            "[project]\nname = \"demo\"\nrepo = \"o/demo\"\n[goal]\nmilestone = \"M\"\n[checks]\n"
            f"test = '\"{sys.executable}\" \"{script}\"'\nmax_minutes = 0.1\n")
        code, out = self.run_check()
        self.assertEqual(code, 3)
        self.assertIn("suite ran out of time after 0.1 min", out)
        self.assertIn("report TESTS: INCOMPLETE", out)
        self.assertFalse(self.lock.exists())
        pid = int(child.read_text())
        for _ in range(50):
            if not alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(alive(pid))

    def test_timeout_kills_the_whole_tree_even_when_taskkill_is_refused(self):
        pids = self.project / "pids.txt"
        script = self.project / "slow.py"
        script.write_text(
            "import subprocess, sys, time\n"
            "pids = sys.argv[1]\n"
            "if len(sys.argv) > 2:\n"
            "    c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "else:\n"
            "    c = subprocess.Popen([sys.executable, __file__, pids, 'child'])\n"
            "open(pids, 'a').write(str(c.pid) + ' ')\n"
            "time.sleep(60)\n")
        (self.project / "room.toml").write_text(
            "[project]\nname = \"demo\"\nrepo = \"o/demo\"\n[goal]\nmilestone = \"M\"\n[checks]\n"
            f"test = '\"{sys.executable}\" \"{script}\" \"{pids}\"'\nmax_minutes = 0.15\n")
        refused = subprocess.CompletedProcess([], 1, b"", b"Access is denied")
        real_run = subprocess.run
        with self.local_check() as out, mock.patch.object(
                cli.subprocess, "run",
                side_effect=lambda cmd, *a, **k: refused if cmd[0] == "taskkill" else real_run(cmd, *a, **k)):
            self.assertEqual(cli.cmd_check(None), 3)
        self.assertIn("report TESTS: INCOMPLETE", out.getvalue())
        self.assertFalse(self.lock.exists())
        self.assertIsNone(claim_lock(self.lock))   # a second claim succeeds at once
        release_lock(self.lock)
        found = [int(x) for x in pids.read_text().split()]
        self.assertEqual(len(found), 2)
        for pid in found:
            for _ in range(50):
                if not alive(pid):
                    break
                time.sleep(0.1)
            self.assertFalse(alive(pid), pid)

    def test_suite_inside_the_limit_passes_its_code(self):
        self.project_with(f"\"{self.project / 'log.txt'}\" 0 5", "max_minutes = 1\n")
        self.assertEqual(self.run_check()[0], 5)

    def test_default_limit_is_nine_minutes_and_ignores_wall_clock_changes(self):
        self.project_with(f"\"{self.project / 'log.txt'}\" 0 7")
        with self.local_check(), mock.patch.object(cli.time, "monotonic", return_value=100), \
                mock.patch.object(cli.time, "time", side_effect=AssertionError("wall clock used")), \
                mock.patch.object(cli.jobtree, "start") as start:
            start.return_value = (mock.Mock(), None)
            start.return_value[0].wait.return_value = 7
            self.assertEqual(cli.cmd_check(None), 7)
        start.return_value[0].wait.assert_called_once_with(timeout=540)
        self.assertFalse(self.lock.exists())

    def test_waiting_leaves_only_the_remaining_time_for_the_suite(self):
        self.project_with(f"\"{self.project / 'log.txt'}\" 0 7",
                          "max_minutes = 0.1\nlock_poll_seconds = 4\n")
        self.lock.parent.mkdir(parents=True)
        self.lock.write_text(str(os.getpid()))
        now = [100]

        def sleep(seconds):
            now[0] += seconds
            release_lock(self.lock)   # the holder lets go after the first poll

        with self.local_check(), mock.patch.object(cli.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(cli.time, "sleep", side_effect=sleep), \
                mock.patch.object(cli.jobtree, "start") as start:
            start.return_value = (mock.Mock(), None)
            start.return_value[0].wait.return_value = 7
            self.assertEqual(cli.cmd_check(None), 7)
        start.return_value[0].wait.assert_called_once_with(timeout=2)
        self.assertFalse(self.lock.exists())

    def test_lock_poll_does_not_exceed_either_deadline_or_start_the_suite(self):
        for settings, seconds in (("max_minutes = 0.01\nlock_wait_minutes = 10", 0.6),
                                  ("max_minutes = 10\nlock_wait_minutes = 0.01", 0.6)):
            with self.subTest(settings=settings):
                self.project_with(f"\"{self.project / 'log.txt'}\" 0 0",
                                  settings + "\nlock_poll_seconds = 30\n")
                self.lock.parent.mkdir(parents=True, exist_ok=True)
                self.lock.write_text(str(os.getpid()))
                now = [0]

                def sleep(duration):
                    now[0] += duration
                    release_lock(self.lock)   # free exactly at the deadline: too late to run

                with self.local_check() as out, \
                        mock.patch.object(cli.time, "monotonic", side_effect=lambda: now[0]), \
                        mock.patch.object(cli.time, "sleep", side_effect=sleep) as poll, \
                        mock.patch.object(cli.jobtree, "start") as start:
                    self.assertEqual(cli.cmd_check(None), 3)
                poll.assert_called_once_with(seconds)
                start.assert_not_called()
                self.assertIn("suite lock busy: report TESTS: INCOMPLETE", out.getvalue())

    def test_expired_allowance_never_starts_a_process(self):
        with mock.patch.object(cli.time, "monotonic", return_value=100), \
                mock.patch.object(cli.jobtree, "start") as start:
            self.assertIsNone(cli._run_limited("unused", 100))
        start.assert_not_called()

    def test_start_failure_releases_the_lock(self):
        self.project_with(f"\"{self.project / 'log.txt'}\" 0 0")
        with self.local_check(), mock.patch.object(cli.jobtree, "start", side_effect=OSError("cannot start")):
            with self.assertRaisesRegex(OSError, "cannot start"):
                cli.cmd_check(None)
        self.assertFalse(self.lock.exists())

    def test_lock_wait_is_capped_by_max_minutes(self):
        self.project_with(f"\"{self.project / 'log.txt'}\" 0 0",
                          "max_minutes = 0.03\nlock_wait_minutes = 10\nlock_poll_seconds = 0.2\n")
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.lock.parent.mkdir(parents=True)
            self.lock.write_text(str(holder.pid))
            started = time.time()
            code, out = self.run_check()
        finally:
            holder.kill(); holder.wait()
        self.assertEqual(code, 3)
        self.assertIn("suite lock busy: report TESTS: INCOMPLETE", out)
        self.assertLess(time.time() - started, 25)

    def test_claim_and_release(self):
        path = Path(self.tmp.name) / "x.lock"
        self.assertIsNone(claim_lock(path))
        self.assertEqual(claim_lock(path), os.getpid())
        release_lock(path)
        self.assertFalse(path.exists())


class CheckPrompts(unittest.TestCase):
    TEST = "python -m unittest discover -s tests"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, checks):
        cfg = config.merge(config.DEFAULTS, {"project": {"name": "p", "repo": "o/p"}, "goal": {"milestone": "M"},
                                             "checks": checks})
        cfg["_root"] = self.tmp.name
        return dispatch.Dispatcher(cfg, self.ledger, board=None, log=lambda *_: None)

    def prompts(self, d):
        issue = {"number": 7, "state": "OPEN", "title": "t", "labels": [], "comments": []}
        d.board = type("B", (), {"pr_for_branch": lambda self, b: None, "comment": lambda *a: None})()
        build = d.prompt(dispatch.Job("build", "builder", issue=issue), "i7-thing")
        review = d.prompt(dispatch.Job("review", "reviewer", pr={"number": 5, "headRefName": "i7-thing"}), None)
        return build, review

    def test_prompts_name_check_and_keep_the_tests_line(self):
        d = self.dispatcher({"test": self.TEST, "ci": False})
        build, review = self.prompts(d)
        room_py = (Path(d.room_root) / "room.py").as_posix()
        for text in (build, review):
            self.assertIn(f'python "{room_py}" check', text)
            self.assertIn("means unfinished: `TESTS: INCOMPLETE` / `REVIEW: INCOMPLETE`, never FAIL", text)
        self.assertIn(f"`TESTS: PASS <sha> {self.TEST}: ", review)
        self.assertTrue(dispatch.tested_on_head(f"[agent:reviewer] REVIEW: APPROVE\nTESTS: PASS abc1234 {self.TEST}: Ran 3 tests ... OK",
                                                "abc1234", self.TEST))

    def test_no_test_command_no_check_hint(self):
        build, review = self.prompts(self.dispatcher({"ci": False}))
        self.assertNotIn(" check`", build + review)


if __name__ == "__main__":
    unittest.main()
