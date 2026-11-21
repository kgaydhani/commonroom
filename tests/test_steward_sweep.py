"""commonroom#326: housekeeping runs the steward's invariants every pass, keeps the findings in the ledger and the
digest, and wakes the director once for those an agent can fix. Fake board, temporary ledger and home."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dispatch, handshake, registry
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with, issue


def pr(n, branch):
    return {"number": n, "headRefName": branch, "title": "t", "labels": [], "state": "OPEN"}


class Counting(FakeBoard):
    """Counts every board call a pass makes."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = []

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if callable(value) and name in ("all_issues", "open_issues", "open_prs", "pr_detail", "set_labels", "comment"):
            object.__getattribute__(self, "calls").append(name)
        return value


class Sweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")
        self.addCleanup(self.ledger.close)

    def dispatcher(self, board, **kw):
        return dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=kw.get("log", lambda *_: None))

    def test_a_housekeeping_pass_records_findings_and_a_clean_pass_clears_them(self):
        lines = []
        board = FakeBoard([issue(5, ["bug"]), issue(6, [], state="CLOSED")], prs=[pr(60, "i6-done")], details={60: {"commits": [], "comments": [], "statusCheckRollup": []}})
        d = self.dispatcher(board, log=lines.append)
        d.housekeeping()
        found = {(f["invariant"], f["number"]) for f in self.ledger.findings("t")}
        self.assertEqual(found, {("unplaced", 5), ("orphan-pr", 60)})
        self.assertEqual(sum("steward:" in l for l in lines), 2)
        self.assertIsNotNone(self.ledger.marked("t", Ledger.SWEEP))
        board.issues[0]["labels"] = [{"name": "parked"}]
        board.prs.clear()
        d.housekeeping()
        self.assertEqual(self.ledger.findings("t"), [])

    def test_a_dry_run_records_nothing(self):
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard([issue(5, ["bug"])]),
                                log=lambda *_: None, dry_run=True)
        d.housekeeping()
        self.assertEqual(self.ledger.findings("t"), [])

    def test_a_finding_that_stays_keeps_its_first_time(self):
        self.ledger.record_findings("t", [{"invariant": "unplaced", "number": 5, "detail": "a"}], now=100)
        self.ledger.record_findings("t", [{"invariant": "unplaced", "number": 5, "detail": "b"}], now=900)
        self.assertEqual([(f["detail"], f["since"]) for f in self.ledger.findings("t")], [("b", 100)])

    def test_the_sweep_adds_no_board_calls(self):
        board = Counting([issue(5, ["bug"]), issue(6, ["status:ready", "role:builder"])], prs=[pr(60, "x-other")])
        self.dispatcher(board).housekeeping()
        with_sweep = list(board.calls)
        board.calls.clear()
        with mock.patch.object(dispatch.steward, "check", side_effect=AssertionError("off")):
            self.dispatcher(board).housekeeping()
        self.assertEqual(with_sweep, board.calls)

    def test_a_failing_sweep_does_not_stop_the_pass(self):
        with mock.patch.object(dispatch.steward, "check", side_effect=RuntimeError("boom")):
            actions = self.dispatcher(FakeBoard([issue(5, ["bug"])])).housekeeping()
        self.assertFalse(any("failed" in a for a in actions))

    def test_the_director_is_told_of_an_unplaced_issue_once(self):
        board = FakeBoard([issue(5, ["bug"]), issue(8, ["status:ready", "role:builder"])])
        d = self.dispatcher(board)
        self.assertIsNone(d.director_trigger())
        d.housekeeping()
        self.assertIn("#5 is open in the milestone with no status", d.director_trigger())
        d.housekeeping()
        self.assertIn("#5", d.director_trigger())         # still new: no director has run since
        run = self.ledger.start_run("t", None, "director", "claude")
        self.ledger.finish_run(run, "done")
        d.housekeeping()                                  # unchanged finding: not repeated
        self.assertIsNone(d.director_trigger())

    def test_overdue_and_stale_findings_do_not_wake_the_director(self):
        board = FakeBoard([issue(7, ["owner:human"], body="Due: 2020-01-01")])
        d = self.dispatcher(board)
        d.housekeeping()
        self.assertEqual([f["invariant"] for f in self.ledger.findings("t")], ["owner-overdue"])
        self.assertIsNone(d.director_trigger())


class Digest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(Path(tmp.name) / "home")})
        env.start()
        self.addCleanup(env.stop)
        folder = Path(tmp.name) / "alpha"
        folder.mkdir()
        (folder / "room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
        self.reg = registry.Registry()
        self.reg.projects["alpha"] = registry.Project("alpha", str(folder), 30)
        self.ledger = Ledger()
        self.addCleanup(self.ledger.close)

    def digest(self):
        return handshake.build_digest(self.reg, self.reg.projects["alpha"], self.ledger, now=1_800_000_000)

    def test_no_sweep_recorded_leaves_the_digest_as_it_was(self):
        self.assertNotIn("Board check", self.digest())
        self.assertTrue(self.digest().endswith("\n") and not self.digest().endswith("\n\n"))

    def test_the_digest_lists_the_latest_findings_or_says_nothing_is_stale(self):
        self.ledger.record_findings("alpha", [{"invariant": "unplaced", "number": 5, "detail": "#5 has no label."}])
        self.assertIn("## Board check, latest sweep\n- #5 has no label.\n", self.digest())
        self.ledger.record_findings("alpha", [])
        self.assertIn("## Board check, latest sweep\nNothing stale.\n", self.digest())


if __name__ == "__main__":
    unittest.main()
