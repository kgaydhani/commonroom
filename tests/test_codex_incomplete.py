"""#457: an unfinished sandbox suite can use the room's test-before-review path."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dispatch, workers
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with, issue, owned


REPORT = "PR_TITLE: Finish the feature\nPR_BODY:\nFocused tests passed.\nTESTS: INCOMPLETE suite timed out\nSTATUS: BLOCKED"


class CodexIncomplete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        self.addCleanup(self.ledger.close)

    def dispatcher(self, enabled=True, provider="codex", ci=False, test="python -m unittest"):
        cfg = cfg_with(self.tmp.name)
        cfg["checks"] = {"test_before_review": enabled, "ci": ci, "test": test}
        work = issue(7, ["status:in-progress", "role:builder"])
        board = FakeBoard([work])
        board.pr_for_branch = lambda branch: None
        d = dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None)
        d._provider, d._worktree = provider, Path(self.tmp.name)
        d.remote_head = lambda branch: None
        return d, board, dispatch.Job("build", "builder", issue=work)

    def run_build(self, enabled=True, report=REPORT, changed=True, provider="codex"):
        d, board, job = self.dispatcher(enabled, provider)
        result = workers.WorkerResult(ok=True, final_text=report)
        calls = []

        def git(args, cwd):
            calls.append(args)
            return " M feature.py" if args[0] == "status" and changed else ""

        def gh(args):
            self.assertEqual(args[:2], ["pr", "create"])
            self.assertIn("TESTS: INCOMPLETE", args[args.index("--body") + 1])
            board.pr_for_branch = lambda branch: owned({"number": 8, "state": "OPEN"}, pr=True)
            d.remote_head = lambda branch: "new-head"

        with mock.patch.object(dispatch, "git", side_effect=git), mock.patch.object(dispatch.board_mod, "gh", side_effect=gh) as publish:
            d.publish_sandboxed_work(job, d._worktree, "i7-feature", result)
            status, outputs, _ = d.assess(job, "i7-feature", "old-head", "", result)
        return status, outputs, board.label_calls, publish.call_count, calls

    def test_incomplete_suite_is_published_and_sent_to_review(self):
        status, outputs, labels, published, calls = self.run_build()
        self.assertEqual((status, published), ("done", 1))
        self.assertEqual(outputs, [{"kind": "pr", "ref": "PR #8"}])
        self.assertIn("status:review", labels[0][1])
        self.assertFalse(any("provider:claude" in add for _, add, _ in labels))
        self.assertTrue(any(c[0] == "commit" for c in calls))
        self.assertTrue(any(c[0] == "push" for c in calls))

    def test_incomplete_marker_can_appear_inside_the_report_prose(self):
        report = REPORT.replace("TESTS: INCOMPLETE", "Full suite: `TESTS: INCOMPLETE`")
        status, _, _, published, _ = self.run_build(report=report)
        self.assertEqual((status, published), ("done", 1))

    def test_other_blocked_builds_keep_the_handoff(self):
        cases = [(False, REPORT, True), (True, REPORT, False),
                 (True, REPORT.replace("TESTS: INCOMPLETE", "TESTS: FAIL"), True)]
        for blocker in ("Blocked: missing credentials", "BLOCKER: missing input", "  blocked: missing input",
                        "BLOCKER missing input", "BLOCKERS: missing input"):
            cases.append((True, REPORT.replace("STATUS:", blocker + "\nSTATUS:"), True))
        for enabled, report, changed in cases:
            with self.subTest(enabled=enabled, report=report, changed=changed):
                status, _, labels, published, calls = self.run_build(enabled, report, changed)
                self.assertEqual((status, published), ("blocked", 0))
                self.assertIn("provider:claude", labels[0][1])
                self.assertFalse(any(c[0] in ("commit", "push") for c in calls))

    def test_claude_blocked_build_still_escalates(self):
        status, _, labels, published, _ = self.run_build(provider="claude")
        self.assertEqual((status, published), ("blocked", 0))
        self.assertIn("needs:director", labels[0][1])

    def test_assessment_requires_a_new_published_head(self):
        d, board, job = self.dispatcher()
        board.pr_for_branch = lambda branch: owned({"number": 8, "state": "OPEN"}, pr=True)
        d.remote_head = lambda branch: "old-head"
        with mock.patch.object(dispatch, "git", return_value=""):
            status, _, _ = d.assess(job, "i7-feature", "old-head", "", workers.WorkerResult(ok=True, final_text=REPORT))
        self.assertEqual(status, "blocked")
        self.assertIn("provider:claude", board.label_calls[0][1])

    def test_pushed_pr_with_another_blocker_still_hands_off(self):
        d, board, job = self.dispatcher()
        board.pr_for_branch = lambda branch: owned({"number": 8, "state": "OPEN"}, pr=True)
        d.remote_head = lambda branch: "new-head"
        result = workers.WorkerResult(ok=True, final_text=REPORT + "\nBlocked: missing input")
        with mock.patch.object(dispatch, "git", return_value=""):
            status, _, _ = d.assess(job, "i7-feature", "old-head", "", result)
        self.assertEqual(status, "blocked")
        self.assertIn("provider:claude", board.label_calls[0][1])

    def test_both_builds_say_blocked_needs_a_reason_in_either_suite_wording(self):
        for provider in ("claude", "codex"):
            for enabled in (True, False):
                with self.subTest(provider=provider, pretest=enabled):
                    d, _, job = self.dispatcher(enabled, provider)
                    text = d.prompt(job, "i7-feature")
                    self.assertIn("`Blocked: <reason>`", text)
                    self.assertIn("aren't blockers", text)
                    self.assertEqual("before any review" in text, enabled)
                    self.assertEqual("The reviewer runs the suite on the PR head" in text, not enabled)
