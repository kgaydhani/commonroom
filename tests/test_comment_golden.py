"""commonroom#223: the comments the room posts on issues, pinned word for word for fixed inputs. They were written against
the f-strings in dispatch.py and must stay equal now that the words live in commonroom/ways/comments/."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dispatch, workers
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with

RED = {"statusCheckRollup": [{"name": "unit", "conclusion": "FAILURE", "status": "COMPLETED",
                              "completedAt": "2026-01-01T00:00:00Z"}]}
REPO = Path(__file__).resolve().parent.parent


class CommentGolden(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3", synchronous="OFF")
        self.issue = {"number": 7, "title": "t", "labels": [{"name": "status:review"}], "state": "OPEN", "comments": []}
        self.board = FakeBoard([self.issue])
        self.d = dispatch.Dispatcher(cfg_with(self.root), self.ledger, board=self.board, log=lambda *_: None)

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def said(self):
        return [b for n, b in getattr(self.board, "comments", [])]

    def test_red_ci_bounce(self):
        self.d.cfg["checks"] = {"test": "pytest -q"}
        self.d.handle_red_ci({"number": 5}, RED, 7, [self.issue])
        self.assertEqual(self.said(), [
            "CI is red on PR #5: unit. Read it with `gh pr checks 5` and `gh run view <run id> --log-failed`, "
            "fix it on the same branch and run `pytest -q` before pushing."])

    def test_red_ci_bounce_without_a_project_test_command(self):
        self.d.cfg["checks"] = {}
        self.d.handle_red_ci({"number": 5}, RED, 7, [self.issue])
        self.assertIn("and run `the project's tests` before pushing.", self.said()[0])

    def test_merge_main_notes(self):
        for review, tail in (("APPROVE", "its approval stands"), (None, "the review still sees only the PR")):
            self.board.comments = []
            issue = dict(self.issue, labels=[{"name": "status:review"}])
            self.d.back_to_merge_main({"number": 5}, 7, [issue], review)
            self.assertEqual(self.said(), [
                "PR #5 conflicts with main. A merge-main run merges origin/main into the branch, resolves only the "
                f"conflicts and reruns the tests. Nothing else changes, so {tail}."])
        self.board.comments = []
        self.d.back_to_merge_main({"number": 5}, 7, [dict(self.issue, labels=[{"name": "status:review"}])], "CHANGES")
        self.assertEqual(self.said(), [
            "PR #5 conflicts with main. The next Claude builder run merges origin/main into the branch, resolves the "
            "conflicts and reruns the tests."])

    def timeout(self, commits):
        self.d.pushed_commits = lambda branch: commits
        job = dispatch.Job("build", "builder", issue=self.issue)
        self.d.report_timeout(job, "i7-x", workers.WorkerResult(ok=False, final_text="", errors=["timed out after 3600s"]))
        return self.said()

    def test_timeout_comments(self):
        self.assertEqual(self.timeout(2), ["The room ended this run at its 60-minute time limit. Its work was preserved "
                                           "on branch `i7-x`; the next attempt continues from it."])
        self.board.comments = []
        self.assertEqual(self.timeout(0), ["The room ended this run at its 60-minute time limit. Nothing was committed, "
                                           "so the next attempt starts over. If this keeps "
                                           "happening, the issue may need splitting."])

    def test_preserve_work_comment(self):
        wt = self.root / "wt"; wt.mkdir()
        answers = {"status": "M a", "diff": "a", "rev-parse": "abc", "rev-list": "1"}
        job = dispatch.Job("build", "builder", issue=self.issue)
        with mock.patch.object(dispatch, "git", lambda args, cwd: answers.get(args[0], "")):
            self.d.preserve_work(job, wt, "i7-x")
        self.assertEqual(self.said(), ["Unfinished work was preserved on branch `i7-x`; the next attempt continues from it."])

    def test_rescue_comment(self):
        job = dispatch.Job("build", "builder", issue=self.issue)
        with mock.patch.object(dispatch, "git", lambda args, cwd: ""), mock.patch.object(dispatch.time, "time", lambda: 1000):
            self.d.rescue(job, self.root, "i7-x")
        bundle = Path(self.ledger.path).parent / "rescue" / "i7-x-1000.bundle"
        self.assertEqual(self.said(), [f"Pushing this branch failed; its unpushed work was saved as "
                                       f"`{bundle}` (`git fetch <bundle> HEAD:i7-x` restores it)."])

    def handoff(self, changed):
        self.d._provider, self.d._worktree = "codex", self.root
        job = dispatch.Job("build", "builder", issue=self.issue)
        with mock.patch.object(dispatch, "git", lambda args, cwd: "M a" if changed else ""):
            self.assertTrue(self.d.codex_handoff(job, "i7-x", " stuck on gh auth \n"))
        return self.said()

    def test_codex_handoff_comments(self):
        self.assertEqual(self.handoff(True), [
            "Codex ended BLOCKED after changing some or all of this. Its work is on branch `i7-x`. A Claude "
            "builder continues from it: run the gate, finish what's left, open the PR.\n\n```\nstuck on gh auth\n```"])
        self.board.comments = []
        self.assertEqual(self.handoff(False), [
            "Codex ended BLOCKED before changing anything: nothing is on branch `i7-x`. A Claude builder "
            "starts from the issue: build it, run the gate, open the PR.\n\n```\nstuck on gh auth\n```"])

    def assess_failure(self, failures_before):
        for _ in range(failures_before):
            run = self.ledger.start_run(self.d.project, "#7", "build", "claude")
            self.ledger.finish_run(run, "failed")
        self.board.pr_for_branch = lambda branch: None
        self.d.remote_head = lambda branch: None
        job = dispatch.Job("build", "builder", issue=self.issue)
        res = workers.WorkerResult(ok=False, final_text="no luck", errors=["boom"])
        self.d.assess(job, "i7-x", None, "2026-01-01T00:00:00Z", res)
        return self.said()

    def test_assess_failure_comments(self):
        self.assertEqual(self.assess_failure(0), ["Attempt 1 produced no PR. Back to ready.\n\n```\nboom\n```"])

    def test_assess_failure_escalation_comment(self):
        n = self.d.cfg["breakers"]["task_failures"]
        self.assertEqual(self.assess_failure(n - 1), [f"Attempt {n} failed, escalating to the director instead of "
                                                       f"retrying.\n\n```\nboom\n```"])


class CommentsLiveInWays(unittest.TestCase):
    PHRASES = ("Read it with", "resolves only the conflicts and reruns", "time limit.", "Nothing was committed",
               "Codex ended BLOCKED before changing", "Unfinished work was preserved", "unpushed work was saved", "produced no PR",
               "escalating to the director instead of retrying")

    def test_dispatch_py_holds_none_of_the_sentences(self):
        code = (REPO / "commonroom" / "dispatch.py").read_text(encoding="utf-8")
        for phrase in self.PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, code)
                self.assertTrue(any(phrase in t for t in dispatch.COMMENTS.values()), phrase)

    def test_the_templates_keep_the_markers_the_code_looks_for(self):
        merge = dispatch.COMMENTS["merge-main"]
        self.assertIn("{{conflicts_main}}", merge)
        self.assertIn("{{merge_main}}", merge)
        self.assertIn("{{conflicts_main}}", dispatch.COMMENTS["merge-main-changes"])
        rendered = dispatch.fill_way(merge, pr=5, conflicts_main=dispatch.CONFLICTS_MAIN, merge_main=dispatch.MERGE_MAIN,
                                     stands="x")
        self.assertIn(dispatch.CONFLICTS_MAIN, rendered)
        self.assertIn(dispatch.MERGE_MAIN, rendered)

    def test_a_template_without_the_marker_fails_the_check(self):
        for name, marker in (("merge-main", "merge_main"), ("merge-main-changes", "conflicts_main")):
            self.assertIn("{{%s}}" % marker, dispatch.COMMENTS[name])
        self.assertRaises(ValueError, dispatch.check_markers, {**dispatch.COMMENTS, "merge-main": "no marker here"})


if __name__ == "__main__":
    unittest.main()
