import tempfile
import time
import unittest
from pathlib import Path

from commonroom import config, dispatch, gate
from commonroom.ledger import Ledger


class FakeBoard:
    def __init__(self, issues, prs=None, details=None):
        self.issues, self.prs, self.details = issues, prs or [], details or {}
        self.label_calls = []

    def open_issues(self, milestone):
        return [i for i in self.issues if i["state"] == "OPEN"]

    def all_issues(self):
        return self.issues

    def open_prs(self):
        return self.prs

    def pr_detail(self, n):
        return self.details[n]

    def set_labels(self, n, add=(), remove=()):
        self.label_calls.append((n, tuple(add), tuple(remove)))

    def merge(self, n):
        self.merged = n


def issue(n, labels, body="", state="OPEN", comments=(), milestone="M0"):
    return {"number": n, "title": f"Issue {n}", "labels": [{"name": l} for l in labels], "body": body,
            "state": state, "comments": list(comments), "milestone": {"title": milestone} if milestone else None}


def cfg_with(root):
    cfg = config.merge(config.DEFAULTS, {"project": {"name": "t", "repo": "o/t"}, "goal": {"milestone": "M0"},
                                         "roles": {"builder": ["codex", "claude:sonnet"], "director": ["claude:opus"]}})
    cfg["_root"] = str(root)
    return cfg


class PureLogic(unittest.TestCase):
    def test_dependencies_parse_only_dependency_lines(self):
        body = "Depends on #1 and #2.\nSee also #9.\nBlocked by #12 (owner)."
        self.assertEqual(dispatch.dependencies(body), {1, 2, 12})

    def test_review_must_postdate_latest_commit(self):
        detail = {"commits": [{"committedDate": "2026-10-05T12:00:00Z"}],
                  "comments": [{"createdAt": "2026-10-05T11:00:00Z", "body": "REVIEW: APPROVE"}]}
        self.assertIsNone(dispatch.latest_review(detail))
        detail["comments"].append({"createdAt": "2026-10-05T12:30:00Z", "body": "[agent:reviewer] REVIEW: CHANGES - fix x"})
        self.assertEqual(dispatch.latest_review(detail), "CHANGES")

    def test_checks_require_ci(self):
        self.assertEqual(dispatch.checks_state({"statusCheckRollup": []}), "pending")
        self.assertEqual(dispatch.checks_state({"statusCheckRollup": [{"conclusion": "SUCCESS"}]}), "green")
        self.assertEqual(dispatch.checks_state({"statusCheckRollup": [{"conclusion": "FAILURE"}, {"conclusion": "SUCCESS"}]}), "red")

    def test_provider_choice_falls_through_and_excludes(self):
        allowed = {"codex": gate.Verdict(False, "dry"), "claude": gate.Verdict(True, "ok")}
        self.assertEqual(dispatch.choose_provider(["codex", "claude:sonnet"], allowed), ("claude", "sonnet"))
        allowed["codex"] = gate.Verdict(True, "ok")
        self.assertEqual(dispatch.choose_provider(["codex", "claude:sonnet"], allowed, exclude="codex"), ("claude", "sonnet"))

    def test_owner_comments_exclude_agent_and_room_comments(self):
        i = issue(5, ["owner:human"], comments=[{"body": "[room] reminder"}, {"body": "[agent:director] note"}, {"body": "done"}])
        self.assertEqual([c["body"] for c in dispatch.owner_comments(i)], ["done"])


class WithLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def test_progress_guarantee(self):
        now = time.time()
        run = self.ledger.start_run("t", "#1", "builder", "claude", now=now - 3600)
        self.ledger.finish_run(run, "failed", now=now - 3000)
        self.ledger.db.execute("UPDATE runs SET weekly_start=10, weekly_end=22 WHERE id=?", (run,)); self.ledger.db.commit()
        self.assertEqual(dispatch.check_progress(self.ledger, "t", now)["kind"], "no_progress")
        merge = self.ledger.start_run("t", "#1", "merge", "none", now=now - 100)
        self.ledger.finish_run(merge, "done", outputs=[{"kind": "merge", "ref": "PR #3"}], now=now - 100)
        self.assertIsNone(dispatch.check_progress(self.ledger, "t", now))

    def test_housekeeping_unblocks_and_merges(self):
        issues = [issue(1, ["status:review"], state="CLOSED"), issue(2, ["status:blocked"], body="Depends on #1"),
                  issue(3, ["status:blocked"], body="Depends on #1 and #4"), issue(4, ["status:ready"]),
                  issue(5, ["status:blocked", "needs:director"], body="Depends on #1")]
        detail = {"commits": [{"committedDate": "2026-10-05T12:00:00Z"}],
                  "comments": [{"createdAt": "2026-10-05T13:00:00Z", "body": "REVIEW: APPROVE"}],
                  "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        fake = FakeBoard(issues, prs=[{"number": 7, "headRefName": "i4-thing"}], details={7: detail})
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertEqual(fake.merged, 7)
        unblocked = {call[0] for call in fake.label_calls if "status:ready" in call[1]}
        self.assertEqual(unblocked, {2, 3})  # 3 unblocks because the merge closed #4; 5 waits for the director
        self.assertTrue(any("merged PR #7" in a for a in actions))

    def test_director_not_woken_for_work_waiting_on_owner(self):
        issues = [issue(11, ["status:blocked", "role:builder"], body="Blocked by #12"), issue(12, ["owner:human"])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIsNone(d.director_trigger())
        issues.append(issue(20, ["status:blocked", "role:builder"], body="Depends on #99"))
        self.assertIn("nothing is ready", d.director_trigger())

    def test_owner_reply_outranks_builds(self):
        issues = [issue(40, ["status:ready", "role:builder"]),
                  issue(41, ["owner:human"], comments=[{"body": "[agent:director] which phone?", "createdAt": "2026-10-05T10:00:00Z"},
                                                       {"body": "Pixel 8, Android 16", "createdAt": "2026-10-05T12:00:00Z"}])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}
        job, provider, model = d.pick(allowed)
        self.assertEqual((job.kind, provider, model), ("direct", "claude", "opus"))
        self.assertIn("#41", job.reason)
        run = self.ledger.start_run("t", None, "director", "claude")
        self.ledger.finish_run(run, "done", outputs=[{"kind": "director", "ref": "answered #41"}])
        job, _, _ = d.pick(allowed)
        self.assertEqual(job.kind, "build")  # answered; back to building

    def test_owner_comment_already_answered_does_not_wake_director(self):
        issues = [issue(42, ["owner:human"], comments=[{"body": "avg 58.9 fps", "createdAt": "2026-10-05T12:32:00Z"},
                                                       {"body": "[agent:director] Recorded.", "createdAt": "2026-10-05T12:40:00Z"}])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIsNone(d.owner_trigger())

    def test_high_risk_review_goes_to_codex_routine_to_claude(self):
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(reviewer=["claude:sonnet"], reviewer_high_risk=["codex", "claude:opus"])
        detail = {"number": 9, "headRefName": "i50-x", "commits": [{"committedDate": "2026-10-05T12:00:00Z"}], "comments": [],
                  "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        issues = [issue(50, ["status:review", "risk:high"])]
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues, prs=[{"number": 9, "headRefName": "i50-x"}], details={9: detail}), log=lambda *_: None)
        job, provider, _ = d.pick(allowed)
        self.assertEqual((job.kind, provider), ("review", "codex"))
        issues[0]["labels"] = [{"name": "status:review"}]
        job, provider, model = d.pick(allowed)
        self.assertEqual((job.kind, provider, model), ("review", "claude", "sonnet"))

    def test_scout_interleaves_with_builds(self):
        issues = [issue(60, ["status:ready", "role:builder"]), issue(61, ["status:ready", "role:scout"], milestone="M1")]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}
        job, _, _ = d.pick(allowed)
        self.assertEqual((job.role, job.issue["number"]), ("scout", 61))  # never scouted: research goes first
        run = self.ledger.start_run("t", "#61", "scout", "claude"); self.ledger.finish_run(run, "done", outputs=[{"kind": "pr", "ref": "PR #70"}])
        job, _, _ = d.pick(allowed)
        self.assertEqual((job.role, job.issue["number"]), ("builder", 60))  # scouted recently: builds first

    def test_per_provider_dispatchers_split_work_by_policy(self):
        issues = [issue(70, ["status:ready", "role:builder"]),
                  issue(71, ["status:ready", "role:builder", "provider:codex"], milestone="M1")]
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}
        cfg = cfg_with(self.tmp.name)
        cfg["roles"]["builder"] = ["claude:sonnet", "codex"]  # the real policy: Claude first, Codex overflow
        claude = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="claude")
        codex = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="codex")
        job, provider, _ = claude.pick(allowed)
        self.assertEqual((job.issue["number"], provider), (70, "claude"))   # routine build stays on Claude
        job, provider, _ = codex.pick(allowed)
        self.assertEqual((job.issue["number"], provider), (71, "codex"))    # pinned task, even from another milestone
        issues[1]["labels"] = [{"name": "done"}]
        self.assertIsNone(codex.pick(allowed))                               # Codex doesn't poach Claude's routine work
        allowed["claude"] = gate.Verdict(False, "held")
        job, provider, _ = codex.pick(allowed)
        self.assertEqual((job.issue["number"], provider), (70, "codex"))    # ...unless Claude is held: overflow

    def test_director_cooldown(self):
        issues = [issue(30, ["needs:director"])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIn("escalated", d.director_trigger())
        run = self.ledger.start_run("t", None, "director", "claude")
        self.ledger.finish_run(run, "done")
        self.assertIsNone(d.director_trigger())


if __name__ == "__main__":
    unittest.main()
