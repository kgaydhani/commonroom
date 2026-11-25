"""A build sent back by a review resumes its own finished session while the cache is warm (commonroom#208)."""
import time
import unittest

from commonroom import dispatch

import test_resume
from test_resume import SESSION


class ReworkResume(test_resume.Resume):
    def setUp(self):
        super().setUp()
        orig, dispatch.REWORK_RESUME = dispatch.REWORK_RESUME, 3300   # the shipped value is 0 since #609
        self.addCleanup(setattr, dispatch, "REWORK_RESUME", orig)
        self.issue["labels"] = [{"name": "needs:changes"}]
        self.logs = []

    def run_job(self, setup, kind="build", provider="claude"):
        def with_pr(d):
            d.board.pr_for_branch = lambda branch: {"number": 9}
            d.log = self.logs.append
            setup(d)
        return super().run_job(with_pr, kind, provider)

    def done(self, d, **kw):
        kw.setdefault("status", "done")
        kw.setdefault("ago", 600)
        kw.setdefault("note", "ok")
        return self.earlier(d, "#7", **kw)

    def assert_fresh_because(self, setup, reason):
        self.assert_fresh(setup)
        self.assertTrue(any("rework starts fresh" in m and reason in m for m in self.logs), self.logs)

    def test_a_sent_back_build_resumes_with_the_rework_prompt(self):
        seen = self.run_job(self.done)
        self.assertEqual(seen["resume"], SESSION)
        self.assertNotIn("FULL PROMPT", seen["prompt"])
        self.assertIn("PR #9", seen["prompt"])
        self.assertIn("feed000", seen["prompt"])
        self.assertIn("gh pr view 9 --repo o/t --comments", seen["prompt"])
        self.assertIn("The room ends this run after 60 minutes.", seen["prompt"])
        self.assertIn(f"resumed session {SESSION}", self.ledger.recent_runs(limit=1)[0]["note"])

    def test_reviewer_and_merge_rows_in_between_are_fine(self):
        def setup(d):
            self.done(d, ago=900)
            self.earlier(d, "#7", role="reviewer", status="done", ago=300, session="r1")
        self.assertEqual(self.run_job(setup)["resume"], SESSION)

    def test_fresh_past_the_window(self):
        self.assert_fresh_because(lambda d: self.done(d, ago=3400), "ago")

    def test_fresh_when_the_session_file_is_missing(self):
        self.assert_fresh_because(lambda d: self.done(d, file=False), "session file")

    def test_fresh_in_a_different_worktree(self):
        self.assert_fresh_because(lambda d: self.done(d, wt=self.root / "i7-x-123"), "worktree")

    def test_fresh_after_a_later_builder_run(self):
        def setup(d):
            self.done(d, ago=900)
            self.earlier(d, "#7", status="failed", ago=300, session="s2")
        self.assert_fresh_because(setup, "did not end done")

    def test_fresh_for_codex(self):
        self.assert_fresh_because(lambda d: self.done(d, provider="codex"), "not Claude")

    def test_a_codex_job_logs_why(self):
        self.run_job(self.done, provider="codex")
        self.assertTrue(any("rework starts fresh" in m and "not Claude" in m for m in self.logs), self.logs)

    def room_comment(self, body):
        self.issue["comments"] = [{"body": "[room] " + body, "createdAt": "2030-01-01T00:00:00Z"}]

    def test_fresh_when_sent_back_for_merge_main(self):
        self.room_comment(f"PR #9 {dispatch.MERGE_MAIN}")
        self.assert_fresh_because(self.done, "merge-main/CI")

    def test_fresh_when_sent_back_for_conflicts(self):
        self.room_comment(f"PR #9 {dispatch.CONFLICTS_MAIN}")
        self.assert_fresh_because(self.done, "merge-main/CI")

    def test_fresh_when_sent_back_for_red_ci(self):
        self.room_comment("CI is red on PR #9: test.")
        self.assert_fresh_because(self.done, "merge-main/CI")

    def test_a_stopped_run_does_not_log_a_fresh_start(self):
        self.run_job(lambda d: self.earlier(d, "#7", status="stopped", ago=300))
        self.assertFalse(any("rework starts fresh" in m for m in self.logs), self.logs)

    def test_fresh_when_the_policy_is_zero(self):
        orig = dispatch.REWORK_RESUME
        dispatch.REWORK_RESUME = 0
        try:
            self.assert_fresh_because(self.done, "is 0")
        finally:
            dispatch.REWORK_RESUME = orig

    def test_fresh_for_a_third_resume_in_a_row(self):
        def setup(d):
            self.done(d, ago=3000, session="s1", note="ok")
            self.done(d, ago=2000, session="s2", note="resumed session s1 from run x; ok")
            self.done(d, ago=1000, session="s3", note="resumed session s2 from run y; ok")
        self.assert_fresh_because(setup, "two resumes")

    def test_a_second_resume_still_resumes(self):
        def setup(d):
            self.done(d, ago=2000, session="s2", note="ok")
            self.done(d, ago=1000, session="s3", note="resumed session s2 from run y; ok")
        self.assertEqual(self.run_job(setup)["resume"], "s3")

    def test_a_build_without_needs_changes_never_resumes_a_done_run(self):
        self.issue["labels"] = []
        self.assert_fresh(self.done)


if __name__ == "__main__":
    unittest.main()
