"""commonroom#354: `room reconcile` records a verdict on an open issue after a change of direction."""
import io
import contextlib
import tomllib
import unittest
from unittest import mock

from commonroom import cli
from commonroom.dispatch import DIRECTOR_MARK, GitHubBoard
from commonroom.ledger import Ledger
from tests.test_handshake import Room


class FakeBoard:
    def __init__(self, repo):
        self.repo, self.calls = repo, []
        FakeBoard.last = self

    def ensure_label(self, name): self.calls.append(("ensure_label", name))
    def set_labels(self, number, add=(), remove=()): self.calls.append(("labels", number, list(add), list(remove)))
    def comment_as_director(self, number, body): self.calls.append(("comment", number, body))
    def close_not_planned(self, number, body): self.calls.append(("close", number, body))


class Reconcile(Room):
    def setUp(self):
        super().setUp()
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\nrepo = "owner/alpha"\n', encoding="utf-8")
        self.register()
        FakeBoard.last = None
        patch = mock.patch.object(cli, "GitHubBoard", FakeBoard)
        patch.start()
        self.addCleanup(patch.stop)

    def mark(self, issue):
        ledger = Ledger()
        try:
            return ledger.marked("alpha", f"reconciled:{issue}")
        finally:
            ledger.close()

    def test_keep_comments_and_changes_no_labels(self):
        self.assertEqual(self.room("reconcile", "alpha", "7", "keep", "still the goal")[0], 0)
        self.assertEqual(FakeBoard.last.repo, "owner/alpha")
        self.assertEqual(FakeBoard.last.calls, [("comment", 7, "DIRECTION: keep: still the goal")])
        self.assertIsNotNone(self.mark(7))

    def test_park_labels_and_comments(self):
        self.assertEqual(self.room("reconcile", "alpha", "7", "park", "later")[0], 0)
        self.assertEqual(FakeBoard.last.calls, [("ensure_label", "parked"), ("labels", 7, ["parked"], []),
                                                ("comment", 7, "DIRECTION: park: later")])
        self.assertIsNotNone(self.mark(7))

    def test_cancel_closes_with_the_comment(self):
        self.assertEqual(self.room("reconcile", "alpha", "7", "cancel", "no longer wanted")[0], 0)
        self.assertEqual(FakeBoard.last.calls, [("close", 7, "DIRECTION: cancel: no longer wanted")])
        self.assertIsNotNone(self.mark(7))

    def test_bad_verdict_or_missing_reason_records_nothing(self):
        for argv in (("reconcile", "alpha", "7", "drop", "why"), ("reconcile", "alpha", "7", "keep"),
                     ("reconcile", "alpha", "7", "keep", "  ")):
            code, _, err = self.room(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("refused", err)
            self.assertIsNone(FakeBoard.last)
            self.assertIsNone(self.mark(7))

    def test_a_failing_board_records_no_mark(self):
        with mock.patch.object(FakeBoard, "comment_as_director", side_effect=RuntimeError("gh down")):
            with self.assertRaises(RuntimeError):
                self.room("reconcile", "alpha", "7", "keep", "x")
        self.assertIsNone(self.mark(7))

    def test_the_real_board_posts_as_the_director(self):
        with mock.patch("commonroom.dispatch.board_mod.gh") as gh:
            GitHubBoard("o/r").comment_as_director(3, "DIRECTION: keep: x")
            GitHubBoard("o/r").close_not_planned(3, "DIRECTION: cancel: x")
        first, second = (c.args[0] for c in gh.call_args_list)
        self.assertEqual(first[first.index("--body") + 1], f"{DIRECTOR_MARK} DIRECTION: keep: x")
        self.assertIn("not planned", second)
        self.assertEqual(second[second.index("--comment") + 1], f"{DIRECTOR_MARK} DIRECTION: cancel: x")


if __name__ == "__main__":
    unittest.main()
