"""commonroom#453: when the first beat on new code fails, rollback.py moves the live checkout back to the last good sha,
and the update step holds the bad sha until main has a newer one that passes. Temporary git repos only."""
import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_self_serve as selfserve
from commonroom import dispatch

REPO = Path(__file__).resolve().parent.parent


def load_rollback():
    spec = importlib.util.spec_from_file_location("rollback_under_test", REPO / "rollback.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rollback = load_rollback()


class Rollback(selfserve.LiveRepo):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name)                          # the ledger's folder: the room home in these tests
        self.d.board = selfserve.IssueBoard([], {})
        self.a = self.head(self.live).lower()
        self.b = self.push("b.txt")
        self.assertIn("fast-forwarded", self.d.update_live_checkout()[0])     # the update step moves A to B
        self.assertEqual(self.head(self.live).lower(), self.b)

    def push(self, name):
        self.commit(self.dev, name); self.git("push", "-q", "origin", "main", cwd=self.dev)
        return self.head(self.dev).lower()

    def branch(self):
        return self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.live)

    def test_the_update_step_writes_the_sha_it_leaves_and_the_one_it_moves_to(self):
        self.assertEqual((self.home / "last-good-sha").read_text().strip(), self.a)
        self.assertEqual((self.home / "updated-to").read_text().strip(), self.b)

    def test_a_failed_beat_goes_back_to_the_last_good_sha_and_holds_the_new_one(self):
        line = rollback.rollback(self.live, self.home, 1)
        self.assertIn("rolled back", line)
        self.assertEqual(self.head(self.live).lower(), self.a)
        self.assertEqual(self.branch(), "HEAD")                                   # detached
        self.assertEqual((self.home / "held-sha").read_text().strip(), self.b)
        self.assertEqual((self.home / "rollback.log").read_text().strip().splitlines(), [line])
        self.assertIsNone(rollback.rollback(self.live, self.home, 1))            # HEAD is no longer updated-to

    def test_it_does_nothing_on_a_good_beat_a_dirty_tree_or_another_head(self):
        self.assertIsNone(rollback.rollback(self.live, self.home, 0))
        (self.live / "b.txt").write_text("edited")
        self.assertIsNone(rollback.rollback(self.live, self.home, 1))
        self.git("checkout", "--", "b.txt", cwd=self.live)
        (self.home / "updated-to").write_text("f" * 40 + "\n")
        self.assertIsNone(rollback.rollback(self.live, self.home, 1))
        self.assertEqual((self.head(self.live).lower(), self.branch()), (self.b, "main"))
        self.assertFalse((self.home / "held-sha").exists())
        self.assertFalse((self.home / "rollback.log").exists())

    def test_the_update_step_skips_the_held_sha_and_returns_to_main_at_a_newer_one(self):
        rollback.rollback(self.live, self.home, 1)
        actions = self.d.update_live_checkout()
        self.assertTrue(actions[0].startswith(f"live checkout held: first beat failed on {self.b[:7]}"), actions)
        self.assertEqual(self.head(self.live).lower(), self.a)
        issues = self.d.board.issues
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0]["title"].startswith(dispatch.BEAT_RED))
        self.assertIn("needs:director", [l["name"] for l in issues[0]["labels"]])
        self.d.update_live_checkout()                                            # still held: one issue
        self.assertEqual((len(issues), self.head(self.live).lower()), (1, self.a))
        c = self.push("c.txt")
        self.assertIn("fast-forwarded", self.d.update_live_checkout()[0])
        self.assertEqual((self.head(self.live).lower(), self.branch()), (c, "main"))
        self.assertFalse((self.home / "held-sha").exists())
        self.assertEqual((self.home / "last-good-sha").read_text().strip(), self.a)
        self.assertEqual((self.home / "updated-to").read_text().strip(), c)

    def test_with_the_suite_rule_only_a_newer_sha_that_passes_both_checks_moves_it(self):
        dispatch.Dispatcher.suite_on_main = self._suite_on_main
        self.d.main_red = lambda everything, actions: False
        self.d.dry_beat_runner = lambda *a: ("PASS", "ok")
        self.assertTrue(self.d.suite_on_main())
        rollback.rollback(self.live, self.home, 1)
        self.ledger.mark("commonroom", f"main-suite:{self.b}")                  # the held sha passed both: still held
        self.ledger.mark("commonroom", f"main-dryrun:{self.b}")
        self.assertIn("first beat failed", self.d.update_live_checkout()[0])
        self.assertEqual(self.head(self.live).lower(), self.a)
        c = self.push("c.txt")
        self.assertIn("first beat failed", self.d.update_live_checkout()[0])    # C untested yet
        self.ledger.mark("commonroom", f"main-suite:{c}")
        self.assertIn("fast-forwarded", self.d.update_live_checkout()[0])
        self.assertEqual((self.head(self.live).lower(), self.branch()), (c, "main"))
        self.assertFalse((self.home / "held-sha").exists())

    def test_without_a_rollback_a_detached_checkout_is_still_left_alone(self):
        self.git("checkout", "--quiet", "--detach", self.a, cwd=self.live)
        self.assertIn("not main", self.d.update_live_checkout()[0])
        self.assertEqual(self.head(self.live).lower(), self.a)


class StandsAlone(unittest.TestCase):
    def test_it_imports_and_runs_with_commonroom_unimportable(self):
        tree = ast.parse((REPO / "rollback.py").read_text(encoding="utf-8"))
        names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse([n for n in names if n.startswith("commonroom")])
        broken = {k: None for k in sys.modules if k == "commonroom" or k.startswith("commonroom.")} | {"commonroom": None}
        with mock.patch.dict(sys.modules, broken), tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ImportError):
                __import__("commonroom")
            module = load_rollback()
            with mock.patch.dict(os.environ, {"COMMONROOM_HOME": tmp}):
                self.assertEqual(module.room_home(), Path(tmp))
                self.assertEqual(module.main(["1"]), 0)                     # no markers: nothing to roll back
            self.assertFalse((Path(tmp) / "held-sha").exists())


class BeatScript(unittest.TestCase):
    def test_rollback_runs_inside_the_block_only_when_the_beat_fails(self):
        lines = (REPO / "beat.cmd").read_text(encoding="utf-8").splitlines()
        start, end = lines.index("("), len(lines) - 1 - lines[::-1].index(")")
        block = lines[start + 1:end]
        self.assertEqual(block[-1].strip(), "exit /b")
        self.assertFalse([l for l in block if l.strip().lower().startswith(("rem", "::"))])
        beat = [l for l in block if "room.py\" beat" in l]
        self.assertEqual(len(beat), 1)
        self.assertIn(' || ("%PY%" "%~dp0rollback.py" 1 ', beat[0])

    @unittest.skipUnless(os.name == "nt", "beat.cmd is a Windows batch file")
    def test_beat_cmd_calls_rollback_on_failure_only(self):
        for code, called in ((0, False), (3, True)):
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                shutil.copy(REPO / "beat.cmd", tmp / "beat.cmd")
                (tmp / ".commonroom").mkdir()
                (tmp / "room.py").write_text(f"import sys; sys.exit({code})\n")
                (tmp / "rollback.py").write_text("import sys, pathlib\n"
                                                 "pathlib.Path(__file__).with_name('called').write_text(sys.argv[1])\n")
                env = {**os.environ, "USERPROFILE": str(tmp)}
                done = subprocess.run(["cmd", "/c", "call", str(tmp / "beat.cmd")], env=env, capture_output=True)
                self.assertEqual((tmp / "called").exists(), called, code)
                self.assertEqual(done.returncode != 0, called, code)


if __name__ == "__main__":
    unittest.main()
