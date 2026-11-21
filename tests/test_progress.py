import sys
import tempfile
import time
import unittest
from pathlib import Path

from commonroom import config, dispatch, progress, usage
from commonroom.ledger import Ledger


def cfg(root, code):
    return {"_root": root, "progress": {"landed": ["{python}", "-c", code]}}


class Landed(unittest.TestCase):
    def test_unset_gives_no_answer(self):
        a = progress.landed({"_root": "."}, time.time())
        self.assertIsNone(a.count)
        self.assertEqual(a.why, "")

    def test_counts_non_empty_lines(self):
        a = progress.landed(cfg(".", "print('a'); print(); print('b')"), time.time())
        self.assertEqual((a.count, a.lines), (2, ["a", "b"]))

    def test_since_is_filled_with_utc_iso(self):
        a = progress.landed({"_root": ".", "progress": {"landed": [sys.executable, "-c", "import sys; print(sys.argv[1])", "{since}"]}}, 86400 * 365)
        self.assertEqual(a.lines, ["1971-01-01T00:00:00Z"])

    def test_failure_unreadable_and_slow_give_no_answer(self):
        a = progress.landed(cfg(".", "print('x'); raise SystemExit(3)"), time.time())
        self.assertIsNone(a.count); self.assertIn("exited 3", a.why)
        a = progress.landed(cfg(".", "import sys; sys.stdout.buffer.write(bytes([255, 254]))"), time.time())
        self.assertIsNone(a.count); self.assertIn("not readable", a.why)
        a = progress.landed(cfg(".", "import time; time.sleep(5)"), time.time(), timeout=0.5)
        self.assertIsNone(a.count); self.assertIn("ran past", a.why)
        a = progress.landed({"_root": ".", "progress": {"landed": ["no-such-command-246"]}}, time.time())
        self.assertIsNone(a.count); self.assertIn("could not run", a.why)


class Breaker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        self.now = now = time.time()
        run = self.ledger.start_run("t", "#1", "builder", "claude", now=now - 7 * 3600)
        self.ledger.finish_run(run, "failed", now=now - 3000)
        for t, pct in ((now - 3600, 10), (now - 3000, 22)):
            self.ledger.record_usage([usage.observation("claude", "seven_day", pct, now + 86400, "test")], now=t)

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def test_landed_lines_without_a_merge_do_not_trip(self):
        self.assertIsNone(dispatch.check_progress(self.ledger, "t", self.now, landed=2))

    def test_nothing_landed_and_no_merge_trips(self):
        trip = dispatch.check_progress(self.ledger, "t", self.now, landed=0)
        self.assertEqual(trip["kind"], "no_progress")

    def test_no_answer_falls_back_to_merges(self):
        self.assertEqual(dispatch.check_progress(self.ledger, "t", self.now, landed=None)["kind"], "no_progress")
        merge = self.ledger.start_run("t", "#1", "merge", "none", now=self.now - 100)
        self.ledger.finish_run(merge, "done", outputs=[{"kind": "merge", "ref": "PR #3"}], now=self.now - 100)
        self.assertIsNone(dispatch.check_progress(self.ledger, "t", self.now, landed=None))


class Lazy(unittest.TestCase):
    """The landed command runs only when the breaker would otherwise trip (commonroom#313)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3", synchronous="OFF")
        self.now = now = time.time()
        run = self.ledger.start_run("t", "#1", "builder", "claude", now=now - 7 * 3600)
        self.ledger.finish_run(run, "failed", now=now - 3000)
        for t, pct in ((now - 3600, 10), (now - 3000, 22)):
            self.ledger.record_usage([usage.observation("claude", "seven_day", pct, now + 86400, "test")], now=t)
        self.calls = self.root / "calls.txt"
        code = f"open({str(self.calls)!r}, 'a').write('x'); print('a')"
        self.cfg = cfg(str(self.root), code)

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, logs):
        return dispatch.Dispatcher(self.base() | {"progress": self.cfg["progress"]}, self.ledger, board=object(), log=logs.append)

    def base(self):
        return config.merge(config.DEFAULTS, {"project": {"name": "t", "repo": "o/t"}, "goal": {"milestone": "M0"}}) | {"_root": str(self.root)}

    def ran(self):
        return len(self.calls.read_text()) if self.calls.exists() else 0

    def test_under_the_limit_the_command_is_not_run(self):
        trip = dispatch.check_progress(self.ledger, "t", self.now, spend_limit=50,
                                       landed=lambda: self.dispatcher([]).landed_count(self.now))
        self.assertIsNone(trip); self.assertEqual(self.ran(), 0)

    def test_early_or_no_room_work_does_not_run_it(self):
        self.assertIsNone(dispatch.check_progress(self.ledger, "other", self.now, landed=lambda: self.fail("ran")))
        self.assertIsNone(dispatch.check_progress(self.ledger, "t", self.now, min_work=10 ** 9, landed=lambda: self.fail("ran")))

    def test_over_the_limit_it_runs_once_and_its_count_decides(self):
        d = self.dispatcher([])
        self.assertIsNone(dispatch.check_progress(self.ledger, "t", self.now, landed=lambda: d.landed_count(self.now)))
        self.assertEqual(self.ran(), 1)
        self.cfg["progress"]["landed"][-1] = "print()"
        trip = dispatch.check_progress(self.ledger, "t", self.now, landed=lambda: self.dispatcher([]).landed_count(self.now))
        self.assertEqual(trip["kind"], "no_progress")

    def test_a_failing_command_is_logged_and_merges_decide(self):
        logs = []
        self.cfg["progress"]["landed"][-1] = "raise SystemExit(3)"
        d = self.dispatcher(logs)
        self.assertIsNone(d.landed_count(self.now))
        self.assertTrue(any("exited 3" in l and "counting merges" in l for l in logs), logs)
        trip = dispatch.check_progress(self.ledger, "t", self.now, landed=lambda: d.landed_count(self.now))
        self.assertEqual(trip["kind"], "no_progress")

    def test_unset_never_logs_or_runs(self):
        logs = []
        d = dispatch.Dispatcher(self.base(), self.ledger, board=object(), log=logs.append)
        self.assertIsNone(d.landed_count(self.now)); self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
