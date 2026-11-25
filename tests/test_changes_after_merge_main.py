"""A CHANGES review after a merge-main send-back gets the normal rework, not another merge-main run (commonroom#552)."""
import tempfile
import unittest
from pathlib import Path

from commonroom import dispatch
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with
from test_rework_resume import ReworkResume

MERGE_AT, REVIEW_AT = "2030-01-01T12:23:00Z", "2030-01-01T13:10:00Z"
ROOM = f"[room] PR #9 conflicts with main. A {dispatch.MERGE_MAIN} merges origin/main. {dispatch.CONFLICTS_MAIN}"


def issue(extra=()):
    return {"number": 7, "title": "t", "state": "OPEN", "body": "",
            "labels": [{"name": n} for n in ("status:ready", "needs:changes", "role:builder", *extra)],
            "comments": [{"body": ROOM, "createdAt": MERGE_AT}]}


def pr_detail(*comments):
    return {"number": 9, "state": "OPEN", "headRefName": "i7-x", "headRefOid": "abc", "commits": [],
            "comments": [{"body": b, "createdAt": at} for b, at in comments]}


CHANGES = ("REVIEW: CHANGES\nfix the thing", REVIEW_AT)


class ChangesAfterMergeMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3", synchronous="OFF")
        orig, dispatch.REWORK_RESUME = dispatch.REWORK_RESUME, 3300   # resume on: the shipped value is 0 since #609
        self.addCleanup(setattr, dispatch, "REWORK_RESUME", orig)

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, detail, i):
        board = FakeBoard([i], [{"number": 9, "headRefName": "i7-x", "headRefOid": "abc"}], {9: detail})
        d = dispatch.Dispatcher(cfg_with(self.root), self.ledger, board=board, log=lambda *_: None)
        d.board.pr_for_branch = lambda branch: {"number": 9, "state": "OPEN", "headRefOid": "abc"}
        return d

    def marked(self, detail):
        i = issue()
        d = self.dispatcher(detail, i)
        return d, d.mark_changes([i])[0]

    def test_a_merge_main_comment_with_no_later_review_stays_merge_main(self):
        d, i = self.marked(pr_detail())
        self.assertTrue(dispatch.merge_main_only(i))
        self.assertIn("merge-main run only", d.prompt(dispatch.Job("build", "builder", issue=i), "i7-x"))
        self.assertEqual(d.build_spec(i), d.roles.get("merge_main", ["claude:sonnet"]))

    def test_a_review_older_than_the_comment_changes_nothing(self):
        _, i = self.marked(pr_detail(("REVIEW: CHANGES\nold", "2029-12-31T00:00:00Z")))
        self.assertTrue(dispatch.merge_main_only(i))

    def test_a_changes_review_after_the_comment_gets_the_normal_rework(self):
        # The exact state of #532: a merge-main room comment, a CHANGES review after it, no later room comment.
        detail = pr_detail(CHANGES, ("REVIEW: CHANGES\nagain", "2030-01-01T14:32:00Z"))
        d, i = self.marked(detail)
        self.assertFalse(dispatch.merge_main_only(i))
        self.assertFalse(dispatch.conflicts_main(i))
        job = dispatch.Job("build", "builder", issue=i)
        prompt = d.prompt(job, "i7-x")
        self.assertNotIn("merge-main run only", prompt)
        self.assertIn("address every point", prompt)
        self.assertIn("PR #9", prompt)
        self.assertEqual(d.build_spec(i), d.roles.get("builder", ["claude:sonnet"]))

    def test_a_room_comment_after_the_review_wins_again(self):
        d, i = self.marked(pr_detail(CHANGES))
        i["comments"].append({"body": ROOM, "createdAt": "2030-01-01T15:00:00Z"})
        self.assertTrue(dispatch.merge_main_only(i))

    def test_a_ready_issue_is_marked_before_it_is_picked(self):
        i = issue()
        d = self.dispatcher(pr_detail(CHANGES), i)
        self.assertFalse(any(dispatch.merge_main_only(x) for x in d.ready_builds(d.mark_changes(d.board.open_issues(None)))))

    def test_an_unreadable_pr_leaves_the_issue_as_it_was(self):
        i = issue()
        d = self.dispatcher(pr_detail(CHANGES), i)
        d.board.pr_detail = lambda n: 1 / 0
        self.assertTrue(dispatch.merge_main_only(d.mark_changes([i])[0]))

    def test_an_issue_without_a_room_comment_is_not_looked_up(self):
        i = issue(); i["comments"] = []
        d = self.dispatcher(pr_detail(CHANGES), i)
        d.board.open_prs = lambda: 1 / 0
        self.assertEqual(d.mark_changes([i]), [i])
        self.assertNotIn("changes_at", i)


class ReworkAfterChanges(ReworkResume):
    def with_review(self, body):
        self.room_comment(body)
        self.issue["changes_at"] = "2031-01-01T00:00:00Z"   # a CHANGES review after the room comment

    def test_a_changes_review_after_a_merge_main_comment_resumes(self):
        self.with_review(f"PR #9 {dispatch.MERGE_MAIN}")
        self.assertEqual(self.run_job(self.done)["resume"], "59facac2-aaaa")
        self.assertFalse(any("merge-main/CI" in m for m in self.logs), self.logs)

    def test_a_changes_review_after_a_red_ci_comment_resumes(self):
        self.with_review("CI is red on PR #9: test.")
        self.run_job(self.done)
        self.assertFalse(any("merge-main/CI" in m for m in self.logs), self.logs)


if __name__ == "__main__":
    unittest.main()
