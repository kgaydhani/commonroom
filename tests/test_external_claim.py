import tempfile
import time
import unittest
from pathlib import Path

from commonroom import dispatch, gate
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, LabelBoard, cfg_with, issue


class ExternalClaim(unittest.TestCase):
    """commonroom#132: on 17 Oct the room released the demo lanes the interactive director had handed to local agents and
    cloud sessions as stale claims, and sent the cloud merge queue's issues back to ready four minutes after they were
    held. `claim:external` keeps the room off an issue someone else owns, while its PR is still reviewed and merged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, board):
        return dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None)

    @staticmethod
    def pr(n, branch, **detail):
        return {"number": n, "headRefName": branch, "mergeable": "MERGEABLE", "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "commits": [{"oid": f"{n}a", "committedDate": "2026-10-07T02:00:00Z"}], "comments": [], **detail}

    def test_a_ready_external_issue_is_never_built(self):
        issues = [issue(666, ["status:ready", "role:builder", "prio:top", dispatch.EXTERNAL]),
                  issue(667, ["status:ready", "role:builder", "prio:low"])]
        d = self.dispatcher(FakeBoard(issues))
        self.assertEqual([i["number"] for i in d.ready_builds(issues)], [667])
        job, _, _ = d.pick({"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "x")})
        self.assertEqual(job.issue["number"], 667)

    def test_an_external_claim_is_never_stale(self):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 3600))
        held = issue(607, ["status:in-progress", dispatch.EXTERNAL]) | {"updatedAt": stamp}
        self.assertEqual(dispatch.stale_claims([held], [], [], time.time()), [])
        board = FakeBoard([held])
        actions = self.dispatcher(board).housekeeping()
        self.assertEqual(board.label_calls, [])
        self.assertFalse(any("stale" in a for a in actions))

    def test_a_conflicting_pr_is_noted_once_but_not_relabelled(self):
        issues = [issue(429, ["status:review", dispatch.EXTERNAL])]
        board = LabelBoard(issues, prs=[{"number": 629, "headRefName": "i429-fog"}],
                           details={629: self.pr(629, "i429-fog", mergeable="CONFLICTING")})
        board.comment = lambda n, body: (FakeBoard.comment(board, n, body),
                                         issues[0]["comments"].append({"createdAt": "2026-10-07T03:00:00Z", "body": body}))
        d = self.dispatcher(board)
        actions = d.housekeeping()
        self.assertEqual(board.label_calls, [])
        self.assertEqual(len(board.comments), 1)
        self.assertIn("PR #629 conflicts with main", board.comments[0][1])
        self.assertTrue(any("#429 is claim:external" in a for a in actions))
        d.housekeeping()                                   # the next pass doesn't repeat it
        self.assertEqual((len(board.comments), board.label_calls), (1, []))

    def test_changes_requested_doesnt_bounce_it_to_ready(self):
        issues = [issue(663, ["status:review", dispatch.EXTERNAL])]
        detail = self.pr(671, "i663-causeway", comments=[{"createdAt": "2026-10-07T02:30:00Z",
                                                          "body": "[agent:reviewer] REVIEW: CHANGES"}])
        board = FakeBoard(issues, prs=[{"number": 671, "headRefName": "i663-causeway"}], details={671: detail})
        self.dispatcher(board).housekeeping()
        self.assertFalse(any("status:ready" in add for _, add, _ in board.label_calls))

    def test_red_ci_is_noted_but_not_bounced(self):
        issues = [issue(659, ["status:review", dispatch.EXTERNAL])]
        detail = self.pr(681, "i659-cloud", statusCheckRollup=[
            {"name": "Fast tests", "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/t/actions/runs/11/job/1"}])
        board = FakeBoard(issues, prs=[{"number": 681, "headRefName": "i659-cloud"}], details={681: detail})
        self.dispatcher(board).housekeeping()
        self.assertEqual(board.label_calls, [])
        self.assertIn("CI is red on PR #681", board.comments[0][1])

    def test_its_approved_pr_still_merges(self):
        issues = [issue(608, ["status:review", dispatch.EXTERNAL])]
        detail = self.pr(685, "i608-painterly", comments=[{"createdAt": "2026-10-07T02:30:00Z",
                                                           "body": "[agent:reviewer] REVIEW: APPROVE"}])
        board = FakeBoard(issues, prs=[{"number": 685, "headRefName": "i608-painterly"}], details={685: detail})
        actions = self.dispatcher(board).housekeeping()
        self.assertEqual(board.merged, 685)
        self.assertIn("merged PR #685 (issue #608)", actions)

    def test_external_work_counts_as_busy_for_the_director(self):
        issues = [issue(666, ["status:blocked", "role:builder", dispatch.EXTERNAL])]
        d = self.dispatcher(FakeBoard(issues))
        self.assertIsNone(d.director_trigger())
        issues.append(issue(20, ["status:blocked", "role:builder"], body="Depends on #99"))
        self.assertIsNone(d.director_trigger())            # held work outside the room isn't "nothing is ready"

    def test_an_unstatused_top_issue_stays_unstatused(self):
        issues = [issue(661, ["prio:top", "role:builder", dispatch.EXTERNAL])]
        board = FakeBoard(issues)
        self.dispatcher(board).housekeeping()
        self.assertEqual(board.label_calls, [])


if __name__ == "__main__":
    unittest.main()
