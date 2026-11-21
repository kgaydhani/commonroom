import tempfile
import unittest
from pathlib import Path

from commonroom import dispatch
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with, issue

READY = ["status:ready", "role:builder"]


class Board(FakeBoard):
    def issue_state(self, n):
        return next((i["state"] for i in self.issues if i["number"] == n), None)


class LookBeforeBuilding(unittest.TestCase):
    """commonroom#325: with `[checks] look_before_building`, a kind:feature build needs a closed `Scouted: #N` or a waiver."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def run_check(self, issues, on=True):
        cfg = cfg_with(self.tmp.name)
        if on:
            cfg["checks"] = {"look_before_building": True}
        board = Board(issues)
        d = dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None)
        open_now = board.open_issues(None)
        picked = [i["number"] for i in d.ready_builds(open_now)]
        return picked, d, board

    def feature(self, body="", extra=()):
        return issue(10, READY + ["kind:feature", *extra], body=body)

    def test_a_feature_without_a_scouted_line_is_skipped_and_commented_once(self):
        picked, d, board = self.run_check([self.feature()])
        self.assertEqual(picked, [])
        self.assertEqual(len(board.comments), 1)
        self.assertIn("Scouted: #N", board.comments[0][1])
        d.ready_builds(board.open_issues(None))
        self.assertEqual(len(board.comments), 1)

    def test_it_does_not_comment_again_when_the_issue_already_has_the_comment(self):
        held = {"body": f"[room] {dispatch.SCOUT_HELD}: no line", "author": "x"}
        i = issue(10, READY + ["kind:feature"], comments=[held])
        picked, _, board = self.run_check([i])
        self.assertEqual((picked, getattr(board, "comments", [])), ([], []))

    def test_a_closed_scout_item_lets_it_through(self):
        picked, _, board = self.run_check([self.feature("Scouted: #5"), issue(5, ["role:scout"], state="CLOSED")])
        self.assertEqual(picked, [10])
        self.assertFalse(getattr(board, "comments", []))

    def test_an_open_scout_item_holds_it(self):
        picked, _, board = self.run_check([self.feature("Why\nScouted: #5"), issue(5, ["role:scout"])])
        self.assertEqual(picked, [])
        self.assertIn("not closed", board.comments[0][1])

    def test_the_waiver_label_lets_it_through(self):
        picked, _, _ = self.run_check([self.feature(extra=["scout:waived"])])
        self.assertEqual(picked, [10])

    def test_a_feature_outside_the_goal_milestone_gets_no_comment_and_no_lookup(self):
        other = issue(10, READY + ["kind:feature"], body="Scouted: #5", milestone="M9")
        picked, d, board = self.run_check([other, issue(5, ["role:scout"])])
        self.assertEqual(picked, [])
        self.assertFalse(getattr(board, "comments", []))

    def test_flag_off_or_absent_changes_nothing(self):
        picked, _, board = self.run_check([self.feature()], on=False)
        self.assertEqual(picked, [10])
        self.assertFalse(getattr(board, "comments", []))

    def test_a_bug_and_an_unlabelled_issue_are_never_held(self):
        picked, _, _ = self.run_check([issue(1, READY + ["kind:bug"]), issue(2, READY + ["kind:infra"]), issue(3, READY)])
        self.assertEqual(sorted(picked), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
