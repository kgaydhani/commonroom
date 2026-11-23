"""commonroom#210: with `[checks] test_before_review`, the room runs the suite on a PR head before any reviewer starts,
and a failing head goes straight back to its builder. `carry.run_suite` is faked; the board is a fake."""
import copy
import unittest
from pathlib import Path

from commonroom import carry, config, dispatch, gate
from test_dispatch import issue, owned
from test_self_serve import Base, MergeBoard, project_cfg

HEAD = "a" * 40
CMD = "python -m unittest discover -s tests"
ALLOWED = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}


class Board(MergeBoard):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.posted, self.stored = [], []

    def comment(self, n, body):
        body = f"{dispatch.MARK} {body}"
        self.posted.append((n, body))
        if n in self.details:
            self.stored.append({"createdAt": "2099-01-01T00:00:00Z", "body": body})

    def pr_detail(self, n):
        d = copy.deepcopy(self.details[n])
        d["comments"] = d["comments"] + copy.deepcopy(self.stored)
        return owned(d, pr=True)

    def issue_comments(self, n):
        return [b for k, b in self.posted if k == n]


def pr_detail(head=HEAD, comments=()):
    return {"number": 50, "headRefName": "i7-thing", "headRefOid": head, "statusCheckRollup": [],
            "commits": [{"oid": head, "committedDate": "2026-11-20T12:00:00Z"}], "comments": list(comments)}


class PreReview(Base):
    def setUp(self):
        super().setUp()
        self._run = carry.run_suite
        carry.run_suite = self.stub
        self.calls, self.results = [], []

    def tearDown(self):
        carry.run_suite = self._run
        super().tearDown()

    def stub(self, root, head, command, worktree, lock, max_minutes, **kw):
        self.calls.append(head)
        return self.results.pop(0)

    def make(self, on=True, ci=False, detail=None, labels=("status:review",)):
        cfg = project_cfg(Path(self.tmp.name), ci=ci)
        cfg["checks"]["test"] = CMD
        if on:
            cfg["checks"]["test_before_review"] = True
        cfg["project"]["worktrees"] = str(Path(self.tmp.name) / "wt")
        board = Board([{"number": 50, "headRefName": "i7-thing", "labels": []}], {50: detail or pr_detail()},
                      issues=[issue(7, labels)])
        return self.dispatcher(cfg, board), board

    def fail_line(self):
        line = carry.Line("exit 1: FAILED (failures=2)")
        line.failing = ("FAIL: test_a (t.T.test_a)", "ERROR: test_b (t.T.test_b)")
        return "FAIL", line

    def test_off_by_default_no_run_and_the_reviewer_is_dispatched(self):
        d, board = self.make(on=False)
        d.housekeeping()
        self.assertEqual((self.calls, board.posted), ([], []))
        self.assertEqual(d.pick_review(ALLOWED)[0].pr["number"], 50)

    def test_on_runs_once_posts_once_and_holds_the_reviewer_until_then(self):
        d, board = self.make()
        self.assertIsNone(d.pick_review(ALLOWED))                 # no result for this head yet
        self.results = [("PASS", "Ran 3 tests in 0.1s OK")]
        d.housekeeping()
        self.assertEqual(self.calls, [HEAD])
        (n, body), = board.posted
        self.assertEqual(n, 50)
        self.assertTrue(body.startswith(f"[room] TESTS: PASS {HEAD} {CMD}: Ran 3 tests in 0.1s OK"), body)
        self.assertNotIn("REVIEW:", body)
        (run,) = [r for r in self.ledger.recent_runs("commonroom", limit=20) if r["role"] == "carry"]
        self.assertEqual(run["provider"], "none")
        d.housekeeping()                                          # the result stands: no second run
        self.assertEqual(len(self.calls), 1)

    def test_pass_and_incomplete_let_the_reviewer_start_and_busy_does_not(self):
        for result, starts in (("PASS", True), ("INCOMPLETE", True), ("BUSY", False)):
            with self.subTest(result):
                d, board = self.make()
                self.results = [(result, "line")]
                d.housekeeping()
                self.assertEqual(len(board.posted), 0 if result == "BUSY" else 1)
                self.assertEqual(d.pick_review(ALLOWED) is not None, starts)

    def test_a_fail_starts_no_reviewer_and_sends_the_issue_back_naming_the_tests(self):
        d, board = self.make()
        self.results = [self.fail_line()]
        d.housekeeping()
        self.assertIsNone(d.pick_review(ALLOWED))
        self.assertIn((7, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), board.labels)
        (text,) = board.issue_comments(7)
        self.assertIn("FAIL: test_a (t.T.test_a)", text)
        self.assertIn("ERROR: test_b (t.T.test_b)", text)
        d.housekeeping()
        self.assertEqual(len(self.calls), 1)

    def test_a_carry_fail_goes_back_to_review_not_held(self):
        body = "[room] " + carry.comment("FAIL", HEAD, CMD, "exit 1", "2026-11-20T13:00:00Z", "b" * 40)
        d, board = self.make(detail=pr_detail(comments=[{"createdAt": "2026-11-21T00:00:00Z", "body": body}]))
        d.housekeeping()
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(d.pick_review(ALLOWED))

    def test_room_fails_at_different_heads_escalate_like_changes_verdicts(self):
        rounds = dispatch.POLICY["review_rounds"]
        d, board = self.make()
        for i in range(rounds):
            head = f"{i + 1:x}" * 40
            board.details[50] = pr_detail(head)
            board.issues[0] = issue(7, ["status:review"])
            self.results = [self.fail_line()]
            d.housekeeping()
        last = board.labels[-1]
        self.assertEqual(last[0], 7)
        self.assertIn("status:blocked", last[1])
        self.assertIn("needs:director", last[1])
        self.assertTrue(any(dispatch.ROUNDS_ESCALATED in b for b in board.issue_comments(7)))
        self.assertEqual(sum(1 for l in board.labels if "needs:changes" in l[1]), rounds - 1)

    def test_a_room_pass_alone_never_merges(self):
        d, board = self.make()
        self.results = [("PASS", "Ran 3 tests in 0.1s OK")]
        d.housekeeping()
        d.housekeeping()
        self.assertEqual(board.merged, [])

    def test_a_project_with_ci_is_unaffected_with_the_flag_on(self):
        d, board = self.make(ci=True)
        board.details[50]["statusCheckRollup"] = [{"conclusion": "SUCCESS"}]
        d.housekeeping()
        self.assertEqual((self.calls, board.posted), ([], []))
        self.assertFalse(d.pre_test)

    def test_no_run_while_a_builder_works_on_the_issue(self):
        d, board = self.make()
        run = self.ledger.start_run("commonroom", "#7", "builder", "claude")
        d.housekeeping()
        self.assertEqual(self.calls, [])
        self.ledger.finish_run(run, "done")

    def test_this_repo_turns_it_on_and_only_in_checks(self):
        cfg = config.load(Path(__file__).resolve().parent.parent / "room.toml", follow_host=False)
        self.assertTrue(cfg["checks"]["test_before_review"])


if __name__ == "__main__":
    unittest.main()


class QuotedMarker(PreReview):
    """commonroom#423: a reviewer APPROVE that quotes a `TESTS: FAIL` marker in plain text is refused, and the reason names it."""
    NO_PROOF = "approval without a `TESTS: PASS"
    OWN_MARKER = "approval carries its own TESTS: FAIL or INCOMPLETE marker"

    def assess(self, approve_body, with_room_pass=True):
        room = {"createdAt": "2026-11-21T00:00:00Z", "body": f"[room] TESTS: PASS {HEAD} {CMD}: Ran 3 tests OK"}
        review = {"createdAt": "2099-01-01T00:00:00Z", "body": approve_body}
        d, board = self.make(detail=pr_detail(comments=([room] if with_room_pass else []) + [review]))
        job = dispatch.Job("review", "reviewer", pr={"number": 50, "headRefName": "i7-thing"})
        result = type("R", (), {"final_text": "", "errors": []})()
        return d.assess(job, None, None, "2026-11-22T00:00:00Z", result)

    def test_plain_quote_fails_with_the_new_reason(self):
        status, _, note = self.assess('[agent:reviewer] REVIEW: APPROVE\nThe old text said "Real test failures stay TESTS: FAIL with CHANGES".')
        self.assertEqual(status, "failed")
        self.assertIn(self.OWN_MARKER, note)
        self.assertIn("backticks", note)

    def test_the_same_quote_in_backticks_is_accepted(self):
        status, _, _ = self.assess('[agent:reviewer] REVIEW: APPROVE\nThe old text said `"Real test failures stay TESTS: FAIL with CHANGES"`.')
        self.assertEqual(status, "done")

    def test_no_room_pass_and_no_marker_keeps_the_existing_reason(self):
        status, _, note = self.assess("[agent:reviewer] REVIEW: APPROVE\nLGTM", with_room_pass=False)
        self.assertEqual(status, "failed")
        self.assertIn(self.NO_PROOF, note)
        self.assertNotIn(self.OWN_MARKER, note)
