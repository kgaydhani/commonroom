"""commonroom#470: a claimed:* item gets no run and no label change on any path: role pick, review pick, merge, bounce, unblock."""
import tempfile
import unittest
from pathlib import Path

from commonroom import dispatch, gate
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, LabelBoard, cfg_with, issue

ALLOWED = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}


class ClaimPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        self.addCleanup(self.ledger.close)

    def dispatcher(self, board, cfg=None):
        cfg = cfg or cfg_with(self.tmp.name)
        cfg["checks"] = {"ci": False}
        return dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None)

    def pr(self, body="[agent:reviewer] REVIEW: APPROVE\nTESTS: PASS 224aaaa: Ran 9 tests OK"):
        return {"number": 224, "headRefName": "i200-x", "headRefOid": "224aaaa", "mergeable": "MERGEABLE",
                "statusCheckRollup": [], "commits": [{"oid": "224aaaa", "committedDate": "2026-10-07T02:00:00Z"}],
                "comments": [{"createdAt": "2026-10-07T02:30:00Z", "body": body}]}

    def pr_board(self, labels, body=None):
        detail = self.pr(body) if body else self.pr()
        fake = LabelBoard([issue(200, labels)], prs=[{"number": 224, "headRefName": "i200-x"}], details={224: detail})
        fake.merges = []
        fake.merge = fake.merges.append
        return fake, self.dispatcher(fake)

    def test_on_demand_role_pick_skips_a_claimed_item(self):
        folder = Path(self.tmp.name) / ".claude" / "agents"
        folder.mkdir(parents=True)
        (folder / "analyst.md").write_text("---\nname: analyst\n---\nYou are the analyst.\n", encoding="utf-8")
        cfg = cfg_with(self.tmp.name)
        cfg["roles"]["analyst"] = ["claude:haiku"]
        for labels, want in ((["status:ready", "role:analyst", "claimed:someone"], []), (["status:ready", "role:analyst"], [5])):
            items = [issue(5, labels)]
            d = self.dispatcher(FakeBoard(items), cfg)
            self.assertEqual([i["number"] for i, _ in d.ready_on_demand(items)], want)

    def test_review_pick_skips_the_pr_of_a_claimed_issue(self):
        body = "[agent:builder] done"
        for labels, picked in ((["status:review", "claimed:someone"], False), (["status:review"], True)):
            fake, d = self.pr_board(labels, body)
            self.assertEqual(d.pick_review(ALLOWED) is not None, picked, labels)

    def test_a_tested_approval_on_a_claimed_issue_is_not_merged(self):
        fake, d = self.pr_board(["status:review", "claimed:someone"])
        d.housekeeping()
        self.assertEqual(fake.merges, [])
        fake, d = self.pr_board(["status:review"])
        d.housekeeping()
        self.assertEqual(fake.merges, [224])

    def test_changes_on_a_claimed_issue_leave_its_labels_alone(self):
        body = "[agent:reviewer] REVIEW: CHANGES\nfix it"
        fake, d = self.pr_board(["status:review", "claimed:someone"], body)
        d.housekeeping()
        self.assertEqual(fake.label_calls, [])
        fake, d = self.pr_board(["status:review"], body)
        d.housekeeping()
        self.assertIn((200, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)

    def test_a_claimed_blocked_issue_stays_blocked_when_its_dependency_closes(self):
        for extra, calls in (([], [(2, ("status:ready",), ("status:blocked",))]), (["claimed:someone"], [])):
            fake = FakeBoard([issue(1, [], state="CLOSED"), issue(2, ["status:blocked"] + extra, body="Depends on #1")])
            self.dispatcher(fake).housekeeping()
            self.assertEqual(fake.label_calls, calls)


if __name__ == "__main__":
    unittest.main()
