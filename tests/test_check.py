"""commonroom#180: `room.py check` runs the project's test command under a per-project lock, so parallel workers queue."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from commonroom import config, dispatch
from commonroom.ledger import Ledger
from commonroom.lock import claim_lock, release_lock

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

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
        return p.pid

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
        self.assertIn(f"`TESTS: PASS <sha> {self.TEST}: ", review)
        self.assertTrue(dispatch.tested_on_head(f"[agent:reviewer] REVIEW: APPROVE\nTESTS: PASS abc1234 {self.TEST}: Ran 3 tests ... OK",
                                                "abc1234", self.TEST))

    def test_no_test_command_no_check_hint(self):
        build, review = self.prompts(self.dispatcher({"ci": False}))
        self.assertNotIn(" check`", build + review)


if __name__ == "__main__":
    unittest.main()
