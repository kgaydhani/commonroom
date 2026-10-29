import tempfile
import unittest
from pathlib import Path

from commonroom import dispatch, gate
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with, issue

READY = ["status:ready", "role:builder"]
ALLOWED = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "x")}


class GoldenSerialised(unittest.TestCase):
    """commonroom#52: issues that re-derive shared golden data conflict when built in parallel, so only one
    `risk:golden` issue is in progress or review at a time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, issues):
        return dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)

    def ready(self, issues):
        return [i["number"] for i in self.dispatcher(issues).ready_builds(issues)]

    def test_two_ready_golden_issues_only_one_is_picked_then_the_second(self):
        issues = [issue(1, READY + ["risk:golden"]), issue(2, READY + ["risk:golden"])]
        job, _, _ = self.dispatcher(issues).pick(ALLOWED)
        self.assertEqual(job.issue["number"], 1)
        issues[0]["labels"] = [{"name": "status:in-progress"}, {"name": "risk:golden"}]   # the pick claims it
        d = self.dispatcher(issues)
        self.assertEqual(d.ready_builds(issues), [])
        self.assertIsNone(d.pick(ALLOWED))
        issues[0]["state"] = "CLOSED"
        job, _, _ = self.dispatcher([i for i in issues if i["state"] == "OPEN"]).pick(ALLOWED)
        self.assertEqual(job.issue["number"], 2)

    def test_ready_builds_offers_only_the_top_golden_issue(self):
        issues = [issue(1, READY + ["risk:golden"]), issue(2, READY + ["risk:golden"]), issue(3, READY)]
        self.assertEqual(self.ready(issues), [1, 3])

    def test_codex_never_takes_a_second_golden_issue(self):
        issues = [issue(1, READY + ["risk:golden"]), issue(2, READY + ["risk:golden"]), issue(3, READY + ["prio:low"])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None,
                                only_provider="codex")
        picked = d.pick({"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")})
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0].issue["number"], 3)

    def test_golden_in_review_holds_a_second_but_not_an_unlabelled_issue(self):
        issues = [issue(1, ["status:review", "role:builder", "risk:golden"]), issue(2, READY + ["risk:golden"]),
                  issue(3, READY + ["prio:low"])]
        self.assertEqual(self.ready(issues), [3])
        job, _, _ = self.dispatcher(issues).pick(ALLOWED)
        self.assertEqual(job.issue["number"], 3)

    def test_a_running_golden_build_holds_a_second(self):
        issues = [issue(1, ["status:ready", "role:builder", "risk:golden"]), issue(2, READY + ["risk:golden"])]
        d = self.dispatcher(issues)
        d.busy_tasks = lambda: {"#1"}
        self.assertEqual([i["number"] for i in d.ready_builds(issues)], [])

    def test_merge_main_for_the_golden_issues_own_pr_is_not_held(self):
        note = f"{dispatch.MARK} {dispatch.MERGE_MAIN}"
        issues = [issue(1, ["status:in-progress", "role:builder", "risk:golden"]),
                  issue(2, READY + ["risk:golden", "needs:changes"], comments=[{"body": note}])]
        self.assertEqual(self.ready(issues), [2])

    def test_a_golden_issue_waiting_on_merge_main_holds_a_fresh_one(self):
        note = f"{dispatch.MARK} {dispatch.MERGE_MAIN}"
        issues = [issue(1, READY + ["risk:golden", "needs:changes"], comments=[{"body": note}]),
                  issue(2, READY + ["risk:golden"]), issue(3, READY)]
        self.assertEqual(self.ready(issues), [1, 3])

    def test_without_golden_labels_nothing_changes(self):
        issues = [issue(1, ["status:in-progress", "role:builder"]), issue(2, READY), issue(3, READY + ["prio:high"])]
        self.assertEqual(self.ready(issues), [2, 3])


if __name__ == "__main__":
    unittest.main()
