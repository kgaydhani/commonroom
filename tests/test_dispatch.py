import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from commonroom import config, dispatch, gate, usage, workers
from commonroom.ledger import Ledger


class FakeBoard:
    def __init__(self, issues, prs=None, details=None):
        self.issues, self.prs, self.details = issues, prs or [], details or {}
        self.label_calls = []

    def open_issues(self, milestone):
        return [i for i in self.issues if i["state"] == "OPEN"
                and (not milestone or (i.get("milestone") or {}).get("title") == milestone)]

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

    def comment(self, n, body):
        self.comments = getattr(self, "comments", []) + [(n, body)]

    def close_issue(self, n, body):
        self.closed = getattr(self, "closed", []) + [(n, body)]

    # Merge safety (commonroom#19): by default every branch contains main and main's CI isn't red.
    behind, main_runs = {}, []

    def behind_main(self, detail):
        return self.behind.get(detail.get("number"), 0)

    def update_branch(self, n):
        self.updated = getattr(self, "updated", []) + [n]

    def main_checks(self):
        return self.main_runs

    def create_issue(self, title, body, labels=(), milestone=None):
        self.created = getattr(self, "created", []) + [(title, tuple(labels), milestone)]
        return 900


def issue(n, labels, body="", state="OPEN", comments=(), milestone="M0"):
    return {"number": n, "title": f"Issue {n}", "labels": [{"name": l} for l in labels], "body": body,
            "state": state, "comments": list(comments), "milestone": {"title": milestone} if milestone else None}


def cfg_with(root):
    cfg = config.merge(config.DEFAULTS, {"project": {"name": "t", "repo": "o/t"}, "goal": {"milestone": "M0"},
                                         "roles": {"builder": ["codex", "claude:sonnet"], "director": ["claude:opus"]}})
    cfg["_root"] = str(root)
    cfg["min_free_gb"] = {"room": 0, "project": 0}   # tests mustn't depend on this machine's free space
    cfg["director"]["steer_every_hours"] = 0         # a fresh ledger has no director run; steering has its own test
    return cfg


class PureLogic(unittest.TestCase):
    def test_dependencies_parse_only_dependency_lines(self):
        body = "Depends on #1 and #2.\nSee also #9.\nBlocked by #12 (owner)."
        self.assertEqual(dispatch.dependencies(body), {1, 2, 12})

    def test_dependencies_stop_at_the_end_of_the_list(self):
        # Real gamemirage briefs: prose and asides after the list are not dependencies (commonroom#12).
        cases = {
            "Depends on #89. Performance is checked separately in #132 (D3), so Codex can take this one (it renders).": {89},
            "Depends on #279 (PR #369) and the opponent-AI harvest.": {279},
            "Depends on #127 (C0) and #128 (L2). Headless and data only; Codex can take it.": {127, 128},
            "Depends on: #180 (PR #238).": {180},
            "Depends on #8, #9 and #7.": {7, 8, 9},
            "Depends on #1. Reuse the rendering approach from #4.": {1},
            "Depends on #165, #180, #65, #162": {65, 162, 165, 180},
            "Depends on issues #3 & #4; see #5": {3, 4},
            "Depends on the art pass in #40": set(),
        }
        for line, expected in cases.items():
            self.assertEqual(dispatch.dependencies(line), expected, line)

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
        for t, pct in ((now - 3600, 10), (now - 3500, 11), (now - 3400, 10), (now - 3000, 22)):   # incl. a flapping digit
            self.ledger.record_usage([usage.observation("claude", "seven_day", pct, now + 86400, "test")], now=t)
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

    def test_prose_after_the_dependency_list_does_not_keep_an_issue_blocked(self):
        # gamemirage#131 sat blocked for an hour: #132, named in the prose, is open and depends back on #131.
        issues = [issue(89, [], state="CLOSED"),
                  issue(131, ["status:blocked"], body="Depends on #89. Performance is checked separately in #132 (D3), "
                                                      "so Codex can take this one (it renders)."),
                  issue(132, ["status:blocked"], body="Depends on #131 (D2).")]
        fake = FakeBoard(issues)
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertIn((131, ("status:ready",), ("status:blocked",)), fake.label_calls)
        self.assertFalse(any(call[0] == 132 for call in fake.label_calls))   # #131 is still open
        self.assertIn("unblocked #131 (dependencies [89] closed)", actions)

    def test_director_not_woken_for_work_waiting_on_owner(self):
        issues = [issue(11, ["status:blocked", "role:builder"], body="Blocked by #12"), issue(12, ["owner:human"])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIsNone(d.director_trigger())
        issues.append(issue(20, ["status:blocked", "role:builder"], body="Depends on #99"))
        self.assertIn("nothing is ready", d.director_trigger())

    def test_director_wakes_for_suggestions_and_escalations_outside_the_milestone(self):
        # gamemirage#118 waited 16.6 h: unplaced suggestions have no milestone (commonroom#10).
        issues = [issue(30, ["status:ready", "role:builder"])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        issues.append(issue(118, ["kind:suggestion"], body="[agent:scout] idea", milestone=None))
        self.assertEqual(d.director_trigger(), "#118 is an untriaged owner suggestion")
        issues[-1] = issue(118, ["kind:suggestion", "triaged"], milestone=None)
        self.assertIsNone(d.director_trigger())
        issues.append(issue(119, ["needs:director"], milestone="Later"))
        self.assertEqual(d.director_trigger(), "#119 escalated to the director")
        issues[-1] = issue(119, ["needs:director"], milestone=None)
        self.assertEqual(d.director_trigger(), "#119 escalated to the director")

    def test_nothing_ready_counts_only_the_goal_milestone(self):
        issues = [issue(30, ["status:ready", "role:builder"]), issue(31, [], milestone="Later"),
                  issue(32, [], milestone=None)]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIsNone(d.director_trigger())
        issues[0] = issue(30, ["role:builder"])                        # the goal milestone has work, none ready
        self.assertIn("nothing is ready", d.director_trigger())

    def test_director_steers_every_few_hours_without_another_trigger(self):
        now = time.time()
        cfg = cfg_with(self.tmp.name)
        cfg["director"] = config.DEFAULTS["director"]                  # the default cadence: 3 h
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([issue(30, ["status:ready", "role:builder"])]),
                                log=lambda *_: None)
        last = self.ledger.start_run("t", None, "director", "claude", now=now - 2.5 * 3600)
        self.ledger.finish_run(last, "done", outputs=[{"kind": "director", "ref": "x"}], now=now - 2.4 * 3600)
        self.assertIsNone(d.director_trigger())                        # no other reason, and not 3 h yet
        self.ledger.db.execute("UPDATE runs SET started_at=? WHERE id=?", (now - 3 * 3600 - 60, last)); self.ledger.db.commit()
        self.assertEqual(d.director_trigger(), dispatch.STEER_REASON)  # 3 h on: steer, with no other reason
        job, provider, _ = d.pick({"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")})
        self.assertEqual((job.kind, provider, job.reason), ("direct", "claude", dispatch.STEER_REASON))
        cfg["director"] = {"steer_every_hours": 5}                     # configurable
        self.assertIsNone(d.director_trigger())
        cfg["director"] = {"steer_every_hours": 0}                     # 0 turns it off
        self.assertIsNone(d.director_trigger())

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

    def test_owner_channel_bulletin_does_not_wake_director(self):
        # The studio's daily bulletin starts with an @-mention of the owner (repo "o/t" -> "@o"), not `[agent:`.
        issues = [issue(43, ["kind:suggestion", "triaged"], comments=[
            {"body": "@o Morning bulletin. Nothing needs you.", "createdAt": "2026-10-05T12:32:00Z"}])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIsNone(d.owner_trigger())
        issues[0]["comments"].append({"body": "love the art, hate the lane", "createdAt": "2026-10-05T12:50:00Z"})
        self.assertIn("#43", d.owner_trigger())

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

    def test_audit_schedule(self):
        cfg = cfg_with(self.tmp.name)
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.assertIsNone(d.audit_due())                       # opt-in: no [audit] section, no audits
        cfg["audit"] = {"every_hours": 1}
        run = self.ledger.start_run("t", "#1", "builder", "claude"); self.ledger.finish_run(run, "done", outputs=[{"kind": "pr", "ref": "PR #2"}])
        self.assertIn("periodic audit", d.audit_due())         # activity and no audit yet: due
        a = self.ledger.start_run("t", None, "auditor", "claude"); self.ledger.finish_run(a, "done", outputs=[{"kind": "report", "ref": "x"}])
        self.assertIsNone(d.audit_due())                       # just audited
        self.ledger.db.execute("UPDATE runs SET started_at=started_at-7200"); self.ledger.db.commit()
        self.assertIsNone(d.audit_due())                       # 2h later but nothing happened since: skip (cheap)
        from commonroom import usage
        self.ledger.record_usage([usage.observation("claude", "seven_day_fable", 80, time.time() + 3600, "test")])
        run = self.ledger.start_run("t", "#3", "builder", "claude"); self.ledger.finish_run(run, "done")
        self.assertIsNone(d.audit_due())                       # Fable weekly above 75%: hold

    def test_trouble_brings_the_audit_forward(self):
        cfg = cfg_with(self.tmp.name); cfg["audit"] = {"every_hours": 2}
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        now = time.time()
        a = self.ledger.start_run("t", None, "auditor", "claude", now=now - 2400); self.ledger.finish_run(a, "done", now=now - 2300)
        self.assertIsNone(d.audit_due())                       # 40 min since the audit, all quiet: wait for 2 h
        for i in range(3):
            r = self.ledger.start_run("t", f"#{i}", "builder", "claude", now=now - 600 + i); self.ledger.finish_run(r, "failed", now=now - 500)
        self.assertIn("early audit", d.audit_due())            # 3 wasted runs: look now
        self.ledger.db.execute("UPDATE runs SET started_at=? WHERE id=?", (now - 900, a)); self.ledger.db.commit()
        self.assertIsNone(d.audit_due())                       # but never more often than every 30 min

    def test_heavy_work_goes_to_opus(self):
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(builder=["claude:sonnet"], builder_heavy=["claude:opus"])
        issues = [issue(80, ["status:ready", "role:builder", "size:L"])]
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "x")}
        _, provider, model = d.pick(allowed)
        self.assertEqual((provider, model), ("claude", "opus"))
        issues[0]["labels"] = [{"name": n} for n in ("status:ready", "role:builder", "size:S")]
        _, _, model = d.pick(allowed)
        self.assertEqual(model, "sonnet")

    def test_new_owner_suggestion_gets_fast_lane(self):
        issues = [issue(90, ["kind:suggestion"], body="worried about sonnet"),
                  issue(91, ["kind:suggestion"], body="[agent:scout] try X")]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        reason = d.owner_trigger()
        self.assertIn("#90", reason); self.assertNotIn("#91", reason)   # agent-filed suggestions wait for the slow lane

    def test_conflicting_pr_goes_back_to_builder(self):
        issues = [issue(95, ["status:review"])]
        detail = {"number": 96, "headRefName": "i95-x", "mergeable": "CONFLICTING", "commits": [], "comments": [],
                  "statusCheckRollup": []}
        fake = FakeBoard(issues, prs=[{"number": 96, "headRefName": "i95-x"}], details={96: detail})
        fake.comment = lambda n, body: setattr(fake, "commented", n)
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertTrue(any("merge conflicts" in a for a in actions))
        self.assertIn((95, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)

    def test_red_ci_goes_back_to_builder_and_infra_cancellations_rerun(self):
        issues = [issue(65, ["status:review"]), issue(66, ["status:review"])]
        red = {"number": 85, "headRefName": "i65-x", "mergeable": "MERGEABLE", "commits": [], "comments": [],
               "statusCheckRollup": [{"name": "Fast tests", "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/t/actions/runs/11/job/1"},
                                     {"name": "Slow simulation tests", "conclusion": "SUCCESS"}]}
        stuck = {"number": 86, "headRefName": "i66-y", "mergeable": "MERGEABLE", "commits": [], "comments": [],
                 "statusCheckRollup": [{"name": "Fast tests", "conclusion": "SUCCESS"},
                                       {"name": "Slow simulation tests", "conclusion": "CANCELLED", "detailsUrl": "https://github.com/o/t/actions/runs/22/job/2"}]}
        fake = FakeBoard(issues, prs=[{"number": 85, "headRefName": "i65-x"}, {"number": 86, "headRefName": "i66-y"}],
                         details={85: red, 86: stuck})
        fake.comments, fake.reruns = [], []
        fake.comment = lambda n, body: fake.comments.append((n, body))
        fake.rerun_ci = lambda run_id, minutes_since: fake.reruns.append(run_id) or 1
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertIn((65, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)
        self.assertIn("Fast tests", fake.comments[0][1])
        self.assertEqual(fake.reruns, ["22"])                                  # the cancelled run is rerun, not blamed
        self.assertFalse(any(n == 66 for n, *_ in fake.label_calls))
        self.assertFalse(hasattr(fake, "merged"))
        issues[0]["comments"] = [{"body": "[room] CI is red on PR #85"}] * 3   # third strike: the director looks
        issues[0]["labels"] = [{"name": "status:review"}]
        d.housekeeping()
        self.assertIn((65, ("status:blocked", "needs:director"), ("status:review",)), fake.label_calls)

    def test_codex_takes_second_routine_build_in_parallel(self):
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(builder=["claude:sonnet", "codex"], builder_heavy=["claude:opus"])
        issues = [issue(100, ["status:ready", "role:builder", "size:S"]), issue(101, ["status:ready", "role:builder", "size:S"]),
                  issue(102, ["status:ready", "role:builder", "size:L"])]
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}
        codex = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="codex")
        claude = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="claude")
        job, provider, _ = codex.pick(allowed)
        self.assertEqual((job.issue["number"], provider), (101, "codex"))   # the second routine task
        job, provider, _ = claude.pick(allowed)
        self.assertEqual((job.issue["number"], provider), (100, "claude"))  # Claude keeps the first
        issues.pop(1)
        self.assertIsNone(codex.pick(allowed))                               # one routine task left: it stays Claude's
        busy = self.ledger.start_run("t", "#102", "builder", "claude")       # ...unless Claude is busy elsewhere
        job, _, _ = codex.pick(allowed)
        self.assertEqual(job.issue["number"], 100)
        self.ledger.finish_run(busy, "done", outputs=[{"kind": "pr", "ref": "PR #1"}])
        issues.append(issue(103, ["status:ready", "role:builder", "size:S"]))
        issues.append(issue(104, ["status:ready", "role:builder", "provider:codex"], milestone="M1"))
        job, _, _ = codex.pick(allowed)
        self.assertEqual(job.issue["number"], 104)                           # pinned Codex-only work beats parallel routine
        issues[:] = [issue(110, ["status:ready", "role:builder", "needs:render"]), issue(111, ["status:ready", "role:builder", "kind:art"]),
                     issue(112, ["status:ready", "role:builder", "kind:perf"])]
        job, _, _ = codex.pick(allowed)                                      # Codex renders off-screen (gamemirage#101)...
        self.assertIn(job.issue["number"], (110, 111))
        issues[:] = [issue(112, ["status:ready", "role:builder", "kind:perf"]), issue(113, ["status:ready", "role:builder", "kind:perf"])]
        self.assertIsNone(codex.pick(allowed))                               # ...but device perf work stays with Claude
        held = {"claude": gate.Verdict(False, "5-hour reserve"), "codex": gate.Verdict(True, "ok")}
        anyone = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIsNone(anyone.pick(held))                                 # ...even when Claude is held (gamemirage#121)

    def fresh_readings(self, now):
        for provider in ("claude", "codex"):
            self.ledger.record_usage([usage.observation(provider, "five_hour", 5, now + 3600, "test"),
                                      usage.observation(provider, "seven_day", 5, now + 3 * 86400, "test")], now=now)

    def test_breakers_and_limits_hold_one_provider_only(self):
        now = time.time()
        self.fresh_readings(now)
        cfg = cfg_with(self.tmp.name)
        for i in range(3):   # Codex refused three jobs: usage limit, not the work's fault
            run = self.ledger.start_run("t", f"#{i}", "builder", "codex", now=now - 300 + i)
            self.ledger.finish_run(run, "limited", now=now - 290 + i)
        v = dispatch.verdicts(self.ledger, cfg, now)
        self.assertTrue(v["claude"].allowed and v["codex"].allowed)
        for i in range(3):   # three real empty Codex runs hold Codex, and only Codex
            run = self.ledger.start_run("t", f"#{i}", "builder", "codex", now=now - 200 + i)
            self.ledger.finish_run(run, "failed", now=now - 190 + i)
        v = dispatch.verdicts(self.ledger, cfg, now)
        self.assertTrue(v["claude"].allowed); self.assertFalse(v["codex"].allowed)
        self.assertEqual(self.ledger.open_breakers("t"), [])                  # views never record a trip
        dispatch.verdicts(self.ledger, cfg, now, trip=True)                    # the dispatcher does
        self.assertEqual([b["kind"] for b in self.ledger.open_breakers("t")], ["no_output:codex"])
        self.ledger.clear_breakers("t")
        self.ledger.set_cooldown("claude", now + 600, "usage limit hit")
        v = dispatch.verdicts(self.ledger, cfg, now)
        self.assertFalse(v["claude"].allowed); self.assertIn("resting until", v["claude"].reason)
        self.assertTrue(dispatch.verdicts(self.ledger, cfg, now + 601)["claude"].allowed)
        self.ledger.trip("t", "pause", "owner pause")   # an owner pause still holds everyone
        v = dispatch.verdicts(self.ledger, cfg, now + 601)
        self.assertFalse(v["claude"].allowed or v["codex"].allowed)

    def test_refused_or_stopped_work_goes_back_without_a_failed_attempt(self):
        board = FakeBoard([issue(70, ["status:in-progress", "role:builder"])])
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None)
        job = dispatch.Job("build", "builder", issue=board.issues[0], reason="ready")
        until = time.time() + 900
        status, outputs, note = d.requeue(job, "codex", workers.WorkerResult(ok=False, limited_until=until))
        self.assertEqual((status, outputs), ("limited", []))
        self.assertEqual(self.ledger.cooldown("codex")["until"], until)
        self.assertIn((70, ("status:ready",), ("status:in-progress",)), board.label_calls)
        status, _, note = d.requeue(job, "claude", workers.WorkerResult(ok=False, interrupted="breaker open: x"))
        self.assertEqual(status, "stopped"); self.assertIsNone(self.ledger.cooldown("claude"))
        self.assertEqual(self.ledger.task_failures("t", "#70"), 0)

    def test_claims_are_exclusive(self):
        first = self.ledger.start_run("t", "#5", "builder", "claude", exclusive=True)
        self.assertIsNotNone(first)
        self.assertIsNone(self.ledger.start_run("t", "#5", "builder", "codex", exclusive=True))
        self.assertEqual(self.ledger.running_tasks("t"), {"#5"})
        self.ledger.finish_run(first, "done")
        self.assertIsNotNone(self.ledger.start_run("t", "#5", "builder", "codex", exclusive=True))
        old = self.ledger.start_run("t", "#6", "builder", "claude", now=time.time() - 3 * 3600)   # crashed long ago
        self.assertIsNotNone(self.ledger.start_run("t", "#6", "builder", "claude", exclusive=True))

    def test_second_claude_slot_only_with_deep_queue_and_pace_headroom(self):
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(builder=["claude:sonnet"], builder_heavy=["claude:opus"])
        issues = [issue(n, ["status:ready", "role:builder", "prio:high"] + (["builder:heavy"] if n == 79 else []))
                  for n in (64, 70, 72, 79)]
        slot2 = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="claude", slot=2)
        roomy = {"claude": gate.Verdict(True, "ok", {"weekly_pace": 50, "weekly_used": 9}), "codex": gate.Verdict(False, "dry")}
        self.assertIsNone(slot2.pick(roomy))                                   # slot 1 idle: it takes the top task itself
        self.ledger.start_run("t", "#64", "builder", "claude", exclusive=True)  # slot 1 busy on #64
        job, provider, model = slot2.pick(roomy)
        self.assertEqual((job.kind, job.issue["number"], model), ("build", 70, "sonnet"))
        tight = {"claude": gate.Verdict(True, "ok", {"weekly_pace": 50, "weekly_used": 40}), "codex": gate.Verdict(False, "dry")}
        self.assertIsNone(slot2.pick(tight))                                   # under 15 points of pace headroom
        issues.pop(); issues.pop()
        self.assertIsNone(slot2.pick(roomy))                                   # 2 ready besides the running one: too shallow
        cfg["parallel"]["claude_slots"] = 1
        issues.append(issue(80, ["status:ready", "role:builder"])); issues.append(issue(81, ["status:ready", "role:builder"]))
        self.assertIsNone(slot2.pick(roomy))                                   # slot disabled by config

    def test_runs_left_by_a_dead_dispatcher_are_recovered(self):
        issues = [issue(90, ["status:in-progress", "role:builder"])]
        board = FakeBoard(issues)
        board.comments = []
        board.comment = lambda n, body: board.comments.append((n, body))
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None)
        live = self.ledger.start_run("t", "#91", "builder", "codex")                 # this process: alive
        dead = self.ledger.start_run("t", "#90", "builder", "claude")
        self.ledger.db.execute("UPDATE runs SET pid=999999 WHERE id=?", (dead,)); self.ledger.db.commit()
        old = self.ledger.start_run("t", "PR #5", "reviewer", "claude", now=time.time() - 3 * 3600)
        self.ledger.db.execute("UPDATE runs SET pid=NULL WHERE id=?", (old,)); self.ledger.db.commit()   # pre-pid row
        actions = d.recover_orphans()
        self.assertEqual(len(actions), 2)
        self.assertEqual({r["id"] for r in self.ledger.running_runs("t")}, {live})
        self.assertIn((90, ("status:ready",), ("status:in-progress",)), board.label_calls)
        self.assertEqual(self.ledger.task_failures("t", "#90"), 0)
        self.assertEqual(d.recover_orphans(), [])                                       # once only

    def test_extra_slot_takes_reviews_and_research_while_slot_one_is_busy(self):
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(reviewer=["claude:opus"], reviewer_high_risk=["codex", "claude:opus"], scout=["claude:opus"])
        issues = [issue(108, ["status:review"]), issue(109, ["status:review", "risk:high"]),
                  issue(101, ["status:ready", "role:scout"], milestone=None)]
        detail = lambda n, b: {"number": n, "headRefName": b, "commits": [], "comments": [], "mergeable": "MERGEABLE",
                               "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        board = FakeBoard(issues, prs=[{"number": 209, "headRefName": "i109-y"}, {"number": 208, "headRefName": "i108-x"}],
                          details={208: detail(208, "i108-x"), 209: detail(209, "i109-y")})
        slot2 = dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None, only_provider="claude", slot=2)
        allowed = {"claude": gate.Verdict(True, "ok", {"weekly_pace": 50, "weekly_used": 9}), "codex": gate.Verdict(True, "ok")}
        self.assertIsNone(slot2.pick(allowed))                                 # slot 1 idle: nothing for an extra slot
        self.ledger.start_run("t", "#90", "builder", "claude", exclusive=True)
        job, provider, _ = slot2.pick(allowed)
        self.assertEqual((job.kind, job.pr["number"], provider), ("review", 208, "claude"))   # high-risk #209 stays Codex's
        self.ledger.start_run("t", "PR #208", "reviewer", "claude", exclusive=True)
        job, _, _ = slot2.pick(allowed)
        self.assertEqual((job.role, job.issue["number"]), ("scout", 101))

    def test_unpushed_commits_are_pushed_or_rescued_before_cleanup(self):
        root = Path(self.tmp.name)
        run = lambda *a, cwd=root: dispatch.git(list(a), cwd)
        run("init", "-q", "--bare", "-b", "main", str(root / "origin.git"))
        run("clone", "-q", str(root / "origin.git"), str(root / "main"))
        main = root / "main"
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            run("config", k, v, cwd=main)
        (main / "a.txt").write_text("a")
        run("add", "-A", cwd=main); run("commit", "-qm", "base", cwd=main); run("push", "-q", "origin", "main", cwd=main)
        run("checkout", "-qb", "i92-x", cwd=main)
        (main / "b.txt").write_text("b")
        run("add", "-A", cwd=main); run("commit", "-qm", "work the room committed", cwd=main)   # push never happened
        board = FakeBoard([issue(92, ["status:ready"])]); board.comment = lambda n, body: None
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None)
        job = dispatch.Job("build", "builder", issue=board.issues[0])
        d.preserve_work(job, main, "i92-x")
        self.assertIn("i92-x", run("ls-remote", "--heads", "origin", cwd=main))
        run("remote", "set-url", "origin", str(root / "missing.git"), cwd=main)          # now the push fails...
        (main / "c.txt").write_text("c")
        d.preserve_work(job, main, "i92-x")
        bundles = list((Path(self.ledger.path).parent / "rescue").glob("i92-x-*.bundle"))
        self.assertEqual(len(bundles), 1)                                               # ...so it's bundled locally

    def test_room_yields_while_the_owner_uses_claude_elsewhere(self):
        now = time.time()
        self.fresh_readings(now)
        projects = Path(self.tmp.name) / "projects"
        root = Path(self.tmp.name) / "game"
        mine = projects / dispatch.re.sub(r"[^A-Za-z0-9]", "-", str(root))
        for folder in (mine, Path(str(mine) + "--claude-worktrees-i1-x"), projects / "D--Docs-career-CV"):
            folder.mkdir(parents=True); (folder / "s.jsonl").write_text("{}")
        old = time.time() - 3600
        os.utime(projects / "D--Docs-career-CV" / "s.jsonl", (old, old))
        dispatch.usage._owner_cache.clear()
        self.assertIsNone(dispatch.usage.owner_active(str(root), 20, projects_dir=projects))   # only the room's own sessions
        os.utime(projects / "D--Docs-career-CV" / "s.jsonl", None)
        dispatch.usage._owner_cache.clear()
        self.assertEqual(dispatch.usage.owner_active(str(root), 20, projects_dir=projects), "D--Docs-career-CV")
        cfg = cfg_with(self.tmp.name); cfg["_root"] = str(root)
        orig = dispatch.usage.owner_active
        dispatch.usage.owner_active = lambda *a, **k: "D--Docs-career-CV"
        try:
            v = dispatch.verdicts(self.ledger, cfg, now)["claude"]
            self.assertTrue(v.allowed); self.assertEqual(v.detail["owner_active"], "D--Docs-career-CV")   # 5% used: fine
            self.ledger.record_usage([usage.observation("claude", "five_hour", 75, now + 3600, "test")], now=now)
            v = dispatch.verdicts(self.ledger, cfg, now)["claude"]
            self.assertFalse(v.allowed); self.assertIn("owner is using Claude elsewhere", v.reason)
            self.assertNotIn("reserve", v.reason)                                # running workers aren't stopped
        finally:
            dispatch.usage.owner_active = orig

    def test_a_full_disk_holds_all_work(self):
        cfg = cfg_with(self.tmp.name)
        self.fresh_readings(time.time())
        self.assertTrue(dispatch.verdicts(self.ledger, cfg)["claude"].allowed)
        cfg["min_free_gb"] = {"room": 10**9, "project": 0}
        v = dispatch.verdicts(self.ledger, cfg)
        self.assertFalse(v["claude"].allowed or v["codex"].allowed)
        self.assertIn("disk nearly full", v["claude"].reason)
        self.assertNotIn("reserve", v["claude"].reason)                       # running workers finish
        self.assertFalse(v["claude"].stop_running)
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.ledger.trip("t", "paused_by_owner_hard", "runaway")
        self.assertIn("paused_by_owner_hard", d.stop_reason("claude"))      # a hard pause still stops them

    def test_a_pause_lets_running_workers_finish_unless_hard(self):
        # commonroom#23: an owner pause killed a builder 23 minutes into a review round, and a scout.
        now = time.time()
        self.fresh_readings(now)
        cfg = cfg_with(self.tmp.name)
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.ledger.trip("t", "paused_by_owner", "Owner is chatting with the director; pause all autonomous work")
        v = dispatch.verdicts(self.ledger, cfg)
        self.assertFalse(v["claude"].allowed or v["codex"].allowed)          # nothing new starts...
        self.assertIsNone(d.stop_reason("claude"))                             # ...but running workers finish
        self.assertIsNone(d.stop_reason("codex"))
        self.ledger.trip("t", "paused_by_owner_hard", "a bad merge")         # the owner wants everything down now
        self.assertIn("paused_by_owner_hard", d.stop_reason("claude"))
        self.assertIn("paused_by_owner_hard", d.stop_reason("codex"))
        self.ledger.clear_breakers("t")
        self.ledger.trip("t", "no_output:codex", "three empty runs")          # automatic breakers still stop workers
        self.assertIsNone(d.stop_reason("claude")); self.assertIn("no_output", d.stop_reason("codex"))
        self.ledger.clear_breakers("t")
        self.ledger.record_usage([usage.observation("claude", "five_hour", 50, now + 4.5 * 3600, "test")], now=now)
        self.assertIn("burn cap", dispatch.verdicts(self.ledger, cfg)["claude"].reason)
        self.assertIsNone(d.stop_reason("claude"))                             # a pace hold lets workers finish...
        self.ledger.trip("t", "paused_by_auditor_hard", "runaway")
        self.assertIn("paused_by_auditor_hard", d.stop_reason("claude"))     # ...but can't hide a hard pause
        self.ledger.clear_breakers("t")
        self.ledger.record_usage([usage.observation("claude", "five_hour", 95, now + 3600, "test")], now=now)
        self.ledger.trip("t", "paused_by_owner", "chatting")
        self.assertIn("5-hour reserve", d.stop_reason("claude"))              # the owner's reserve always stops them

    def test_room_pause_is_graceful_and_hard_is_explicit(self):
        from commonroom import cli
        path = Path(self.tmp.name) / "cli.sqlite3"
        orig = (cli.config_mod.load, cli.Ledger)
        cli.config_mod.load, cli.Ledger = (lambda: {"project": {"name": "t"}}), (lambda: Ledger(path))
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cli.main(["pause", "Owner is chatting with the director", "--kind", "paused_by_owner"])
                cli.main(["pause", "--hard", "runaway"])
            self.assertIn("running workers finish", out.getvalue())
        finally:
            cli.config_mod.load, cli.Ledger = orig
        ledger = Ledger(path)
        kinds = [b["kind"] for b in ledger.open_breakers("t")]
        ledger.close()
        self.assertEqual(kinds, ["paused_by_owner", "paused_by_auditor_hard"])
        self.assertEqual([gate.stops_running(k) for k in kinds], [False, True])
        self.assertTrue(gate.stops_running("no_progress") and gate.stops_running("no_output:codex"))

    def test_runs_the_room_stopped_are_not_blamed_on_the_role(self):
        from commonroom import cli
        now = time.time()
        stopped = self.ledger.start_run("t", "#65", "builder", "claude", "opus", now=now - 1500)
        self.ledger.finish_run(stopped, "stopped", note="stopped by the room: breaker open: paused_by_owner_hard", now=now - 108)
        failed = self.ledger.start_run("t", "#66", "builder", "claude", "opus", now=now - 600)
        self.ledger.finish_run(failed, "failed", now=now - 300)
        g = cli.efficiency(self.ledger, "t", now - 3600, now)["by_role_model_effort"]["builder/claude:opus@default"]
        self.assertEqual((g["runs"], g["wasted"], g["stopped"], g["stopped_minutes"]), (2, 1, 1, 23.2))
        cfg = cfg_with(self.tmp.name); cfg["project"]["repo"] = None          # no board calls
        orig = cli.Ledger
        cli.Ledger = lambda: Ledger(self.ledger.path)
        try:
            summary = cli.digest(cfg, 1)["summary"]
        finally:
            cli.Ledger = orig
        self.assertEqual((summary["failed"], summary["stopped_by_room"], summary["minutes_lost_to_stops"]), (1, 1, 23.2))

    def test_director_cooldown(self):
        issues = [issue(30, ["needs:director"])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIn("escalated", d.director_trigger())
        run = self.ledger.start_run("t", None, "director", "claude")
        self.ledger.finish_run(run, "done")
        self.assertIsNone(d.director_trigger())

    def test_escalation_outranks_builds(self):
        issues = [issue(50, ["status:ready", "role:builder"]), issue(51, ["status:blocked", "needs:director"])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}
        job, provider, model = d.pick(allowed)
        self.assertEqual((job.kind, provider, model), ("direct", "claude", "opus"))
        self.assertIn("#51", job.reason)
        run = self.ledger.start_run("t", None, "director", "claude")
        self.ledger.finish_run(run, "done")
        job, _, _ = d.pick(allowed)
        self.assertEqual(job.kind, "build")  # within the cooldown the queue carries on

    def agents(self, *names):
        folder = Path(self.tmp.name) / ".claude" / "agents"
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / f"{name}.md").write_text(f"---\nname: {name}\n---\nYou are the {name}.\n", encoding="utf-8")

    def test_a_configured_role_runs_on_demand(self):
        self.agents("builder", "playtester")
        cfg = cfg_with(self.tmp.name); cfg["roles"]["playtester"] = ["claude:sonnet"]
        issues = [issue(80, ["status:ready", "role:playtester"]), issue(81, ["status:ready", "role:builder"]),
                  issue(82, ["status:ready", "role:playtester"], milestone="M1")]   # outside the goal: never picked
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}
        job, provider, model = d.pick(allowed)
        self.assertEqual((job.kind, job.role, job.issue["number"], provider, model), ("role", "playtester", 80, "claude", "sonnet"))
        self.ledger.start_run("t", "#80", "playtester", "claude")   # claimed: the builder task comes next
        job, _, _ = d.pick(allowed)
        self.assertEqual((job.kind, job.role, job.issue["number"]), ("build", "builder", 81))
        prompt = d.prompt(dispatch.Job("role", "playtester", issue=issues[0]), None)
        for part in ("issue #80", "[agent:playtester]", "kind:bug", "no code PR", "`STATUS: DONE` or `STATUS: BLOCKED`"):
            self.assertIn(part, prompt)

    def test_an_unconfigured_role_is_skipped_and_holds_nothing_up(self):
        self.agents("builder", "playtester", "director")
        issues = [issue(80, ["status:ready", "role:playtester"]), issue(81, ["status:ready", "role:art-tech"]),
                  issue(82, ["status:ready", "role:director"]), issue(83, ["status:ready", "role:builder"])]
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}
        # playtester has an agent file but no [roles] entry; art-tech neither; director is never on demand.
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        job, _, _ = d.pick(allowed)
        self.assertEqual((job.kind, job.role, job.issue["number"]), ("build", "builder", 83))
        # A [roles] entry without an agent file isn't runnable either.
        cfg = cfg_with(self.tmp.name); cfg["roles"]["art-tech"] = ["claude:sonnet"]
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertEqual(d.pick(allowed)[0].issue["number"], 83)

    def test_builder_and_scout_picks_are_unchanged_by_on_demand_roles(self):
        # gamemirage today: analyst is configured, art-tech has an agent file but no [roles] entry, and the board
        # carries role:director and role:art-tech labels. Builds and research go exactly as before.
        self.agents("builder", "scout", "director", "analyst", "art-tech")
        cfg = cfg_with(self.tmp.name); cfg["roles"]["analyst"] = ["claude:haiku"]; cfg["roles"]["codex_parallel_builds"] = False
        issues = [issue(50, ["status:ready", "role:art-tech"]), issue(51, ["status:ready", "role:director"]),
                  issue(60, ["status:ready", "role:builder"]), issue(61, ["status:ready", "role:scout"], milestone="M1")]
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}
        self.assertEqual(d.ready_on_demand(issues), [])
        job, _, _ = d.pick(allowed)
        self.assertEqual((job.kind, job.role, job.issue["number"]), ("build", "scout", 61))
        run = self.ledger.start_run("t", "#61", "scout", "claude"); self.ledger.finish_run(run, "done", outputs=[{"kind": "pr", "ref": "PR #70"}])
        job, _, _ = d.pick(allowed)
        self.assertEqual((job.kind, job.role, job.issue["number"]), ("build", "builder", 60))

    def test_an_on_demand_run_closes_its_issue_with_the_summary(self):
        board = FakeBoard([issue(80, ["status:in-progress", "role:playtester"])])
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None)
        job = dispatch.Job("role", "playtester", issue=board.issues[0], reason="ready")
        status, outputs, _ = d.assess(job, None, None, "", workers.WorkerResult(ok=True, final_text="Looks right.\nSTATUS: DONE"))
        self.assertEqual((status, outputs), ("done", [{"kind": "playtester", "ref": "#80 closed"}]))
        self.assertEqual(len(board.closed), 1)
        self.assertIn("Looks right.", board.closed[0][1]); self.assertNotIn("STATUS", board.closed[0][1])
        status, _, _ = d.assess(job, None, None, "", workers.WorkerResult(ok=True, final_text="No build.\nSTATUS: BLOCKED"))
        self.assertEqual(status, "blocked")
        self.assertIn((80, ("status:blocked", "needs:director"), ("status:in-progress",)), board.label_calls)
        status, _, _ = d.assess(job, None, None, "", workers.WorkerResult(ok=False, errors=["crashed"]))
        self.assertEqual(status, "failed"); self.assertEqual(len(board.closed), 1)
        self.assertIn((80, ("status:ready",), ("status:in-progress",)), board.label_calls)

    def test_digest_lists_every_configured_role(self):
        from commonroom import cli
        self.agents("builder", "director", "playtester")
        cfg = cfg_with(self.tmp.name); cfg["project"]["repo"] = None; cfg["roles"]["playtester"] = ["claude:sonnet"]
        run = self.ledger.start_run("t", "#1", "builder", "claude", now=time.time() - 7200); self.ledger.finish_run(run, "done")
        orig = cli.Ledger
        cli.Ledger = lambda: Ledger(self.ledger.path)
        try:
            since = cli.digest(cfg, 1)["hours_since_last"]
        finally:
            cli.Ledger = orig
        self.assertEqual(since, {"builder": 2.0, "director": None, "playtester": None})

    def test_room_errors_rest_the_task_and_the_third_goes_to_the_director(self):
        # commonroom#9: one leftover folder became five identical room-errors on PR #124 in two minutes.
        root = Path(self.tmp.name)
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".claude" / "agents" / "reviewer.md").write_text("---\nmodel: opus\n---\nReview.\n")
        detail = {"number": 124, "headRefName": "i90-x", "commits": [], "comments": [], "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        board = FakeBoard([issue(90, ["status:review"])], prs=[{"number": 124, "headRefName": "i90-x"}], details={124: detail})
        board.comments = []
        board.comment = lambda n, body: board.comments.append((n, body))
        d = dispatch.Dispatcher(cfg_with(root), self.ledger, board=board, log=lambda *_: None)
        def leftover(job, provider=None):
            raise RuntimeError("git worktree add --detach .../review-124 origin/i90-x: fatal: '.../review-124' already exists")
        d.prepare_worktree = leftover
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}
        later = lambda seconds: (self.ledger.db.execute("UPDATE runs SET started_at=started_at-?, ended_at=ended_at-?",
                                                        (seconds, seconds)), self.ledger.db.commit())
        orig = dispatch.git
        dispatch.git = lambda args, cwd: ""                                    # no fetch from a real remote
        try:
            for attempt in (1, 2, 3):
                job, provider, model = d.pick(allowed)
                self.assertEqual((job.kind, job.pr["number"]), ("review", 124))
                with self.assertRaises(RuntimeError):
                    d.run_job(job, provider, model)
                self.assertIsNone(d.pick(allowed))                             # not retried on the next pass
                later(dispatch.ROOM_ERROR_BACKOFF * 2 ** (attempt - 1) - 60)
                self.assertIsNone(d.pick(allowed))                             # the rest doubles: 10, 20, 40 min
                later(61)
        finally:
            dispatch.git = orig
        self.assertEqual(board.label_calls, [(90, ("needs:director",), ())])     # escalated once, on the third
        self.assertIn("3 room errors in a row", board.comments[0][1])
        self.assertIn("already exists", board.comments[0][1])
        build = dispatch.Job("build", "builder", issue=issue(91, ["status:ready", "role:builder"]))
        for _ in range(3):
            d.room_error(self.ledger.start_run("t", "#91", "builder", "claude"), build, "#91", RuntimeError("boom"))
        self.assertIn((91, ("status:blocked", "needs:director"), ("status:ready", "status:in-progress")), board.label_calls)
        done = self.ledger.start_run("t", "#91", "builder", "claude"); self.ledger.finish_run(done, "done")
        self.assertNotIn("#91", self.ledger.room_error_streaks("t"))           # a run that gets through ends the streak

    def test_review_without_open_pr_goes_back_to_ready(self):
        issues = [issue(60, ["status:review"]), issue(61, ["status:review"])]
        fake = FakeBoard(issues, prs=[{"number": 9, "headRefName": "i61-thing"}],
                         details={9: {"commits": [], "comments": [], "statusCheckRollup": []}})
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertIn((60, ("status:ready",), ("status:review",)), fake.label_calls)
        self.assertFalse(any(call[0] == 61 for call in fake.label_calls))   # its PR is open: still in review
        self.assertTrue(any("#60" in a for a in actions))

    @staticmethod
    def approved_pr(n, branch):
        return {"number": n, "headRefName": branch, "mergeable": "MERGEABLE", "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "commits": [{"oid": f"{n}a", "committedDate": "2026-10-07T02:00:00Z"}],
                "comments": [{"createdAt": "2026-10-07T02:30:00Z", "body": "[agent:reviewer] REVIEW: APPROVE"}]}

    def test_approved_prs_merge_one_at_a_time_and_only_up_to_date(self):
        """commonroom#19: PRs #224 and #225 were each green against the old main, merged 5 s apart, and broke main."""
        issues = [issue(200, ["status:review"]), issue(201, ["status:review"])]
        details = {224: self.approved_pr(224, "i200-front-widths"), 225: self.approved_pr(225, "i201-haze-lights")}
        fake = FakeBoard(issues, prs=[{"number": 225, "headRefName": "i201-haze-lights"},     # newest first, like gh
                                      {"number": 224, "headRefName": "i200-front-widths"}], details=details)
        fake.merges = []
        fake.merge = fake.merges.append
        fake.behind = {}
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)

        d.housekeeping()                                                  # pass 1: the older one, alone
        self.assertEqual(fake.merges, [224])
        fake.prs = fake.prs[:1]
        fake.behind = {225: 1}                                            # main moved under #225
        actions = d.housekeeping()                                        # pass 2: bring it up to date, don't merge
        self.assertEqual(fake.merges, [224])
        self.assertEqual(fake.updated, [225])
        self.assertTrue(any("behind main" in a for a in actions))

        # GitHub's update is a merge commit after the approval. CI reruns on it; the approval stands only because
        # that commit merges main and nothing else, so no reviewer is dispatched for it.
        details[225]["commits"].append({"oid": "225m", "committedDate": "2026-10-07T03:20:00Z"})
        details[225]["statusCheckRollup"] = [{"conclusion": "IN_PROGRESS"}]
        fake.behind = {}
        checked = []
        d.only_merges_main = lambda branch, oids: checked.append((branch, oids)) or True
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}
        self.assertIsNone(d.pick_review(allowed))
        self.assertEqual(checked[-1], ("i201-haze-lights", ["225m"]))
        d.housekeeping()                                                  # CI still running: wait
        self.assertEqual(fake.merges, [224])
        details[225]["statusCheckRollup"] = [{"conclusion": "SUCCESS"}]
        d.housekeeping()                                                  # green on the merged tree: merge
        self.assertEqual(fake.merges, [224, 225])
        d.only_merges_main = lambda branch, oids: False                   # anything more than a merge: review again
        self.assertEqual(d.pick_review(allowed)[0].pr["number"], 225)

    def test_red_main_holds_merges_and_opens_one_fix_task(self):
        """commonroom#19: main stayed red for 1.5 h and nothing in the room noticed."""
        issues = [issue(200, ["status:review"])]
        fake = FakeBoard(issues, prs=[{"number": 224, "headRefName": "i200-x"}],
                         details={224: self.approved_pr(224, "i200-x")})
        fake.merges = []
        fake.merge = fake.merges.append
        fake.main_runs = [{"name": "Fast tests", "conclusion": "failure", "sha": "1f411c0aa",
                           "detailsUrl": "https://github.com/o/t/actions/runs/5/job/1"},
                          {"name": "Slow simulation tests", "conclusion": "success", "sha": "1f411c0aa"}]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertEqual(fake.merges, [])
        self.assertEqual(fake.created, [("Main is red: Fast tests failing on 1f411c0",
                                         ("prio:top", "role:builder", "status:ready", "kind:bug"), "M0")])
        self.assertTrue(any("merges held" in a for a in actions))

        issues.append(issue(900, ["status:review"]) | {"title": "Main is red: Fast tests failing on 1f411c0"})
        fake.prs.append({"number": 901, "headRefName": "i900-fix-main"})
        fake.details[901] = self.approved_pr(901, "i900-fix-main")
        d.housekeeping()                                                  # no second task; only the fix lands
        self.assertEqual(len(fake.created), 1)
        self.assertEqual(fake.merges, [901])

        fake.main_runs = [{"name": "Fast tests", "conclusion": "cancelled", "sha": "2"}]   # a runner hiccup
        d.housekeeping()
        self.assertEqual(fake.merges, [901, 224])                         # isn't a red main

    def test_conflicts_on_an_approved_pr_get_a_cheap_claude_merge_main_run(self):
        """commonroom#14 and #19: a one-line conflict cost a full builder run plus a re-review, and Codex can't fetch."""
        cfg = cfg_with(self.tmp.name)
        cfg["roles"] = {"builder": ["claude:opus", "codex"], "builder_heavy": ["claude:opus"]}
        work = issue(173, ["status:review", "risk:high"])
        changes = issue(174, ["status:review"])
        conflicted = self.approved_pr(180, "i173-header") | {"mergeable": "CONFLICTING"}
        rejected = {**self.approved_pr(181, "i174-x"), "mergeable": "CONFLICTING",
                    "comments": [{"createdAt": "2026-10-07T02:30:00Z", "body": "REVIEW: CHANGES - fix y"}]}
        fake = FakeBoard([work, changes], prs=[{"number": 180, "headRefName": "i173-header"},
                                               {"number": 181, "headRefName": "i174-x"}],
                         details={180: conflicted, 181: rejected})
        d = dispatch.Dispatcher(cfg, self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertIn((173, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)
        notes = dict(fake.comments)
        self.assertIn("merge-main run", notes[173])
        self.assertIn("approval stands", notes[173])
        self.assertNotIn("merge-main run", notes[174])                    # review asked for changes: a full run
        self.assertTrue(any("merge conflicts" in a for a in actions))

        work["labels"] = [{"name": "status:ready"}, {"name": "role:builder"}, {"name": "risk:high"}]
        work["comments"] = [{"body": "[room] " + notes[173]}]
        self.assertEqual(d.build_spec(work), ["claude:sonnet"])          # not Opus for a merge, never Codex
        d.board.pr_for_branch = lambda branch: {"number": 180, "state": "OPEN"}
        d._provider = "claude"
        prompt = d.prompt(dispatch.Job("build", "builder", issue=work), "i173-header")
        self.assertIn("merge-main run", prompt)
        self.assertIn("git merge origin/main", prompt)
        work["comments"].append({"body": "[room] Attempt 1 produced no PR. Back to ready."})
        self.assertEqual(d.build_spec(work), ["claude:opus"])             # the room's latest word decides

        # GitHub refusing to update a behind branch because of conflicts sends it back the same way.
        clean = issue(175, ["status:review"])
        fake.issues, fake.prs = [clean], [{"number": 182, "headRefName": "i175-y"}]
        fake.details = {182: self.approved_pr(182, "i175-y")}
        fake.behind = {182: 3}
        fake.comments = []

        def refuse(n):
            raise RuntimeError("Cannot update PR branch due to conflicts")
        fake.update_branch = refuse
        d.housekeeping()
        self.assertIn("merge-main run", fake.comments[0][1])


    def test_second_changes_verdict_goes_to_the_director_not_the_builder(self):
        """commonroom#32: gamemirage PR #369 went 6+ builder/reviewer rounds on a PR that couldn't change."""
        def changes(t):
            return {"createdAt": t, "body": "[agent:reviewer] REVIEW: CHANGES - still wrong"}
        work = issue(70, ["status:review"])
        detail = {"commits": [{"committedDate": "2026-10-09T09:00:00Z"}], "comments": [changes("2026-10-09T09:30:00Z")],
                  "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        fake = FakeBoard([work], prs=[{"number": 71, "headRefName": "i70-x"}], details={71: detail})
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        to_builder = (70, ("status:ready", "needs:changes"), ("status:review", "status:in-progress"))
        to_director = (70, ("status:blocked", "needs:director"), ("status:review", "status:in-progress", "needs:changes"))

        d.housekeeping()                                                     # round 1: the builder fixes it
        self.assertEqual(fake.label_calls, [to_builder])
        fake.label_calls.clear()
        detail["commits"].append({"committedDate": "2026-10-09T10:00:00Z"})  # round 2: rejected again
        detail["comments"].append(changes("2026-10-09T10:30:00Z"))
        actions = d.housekeeping()
        self.assertEqual(fake.label_calls, [to_director])
        self.assertIn("2 review rounds without an approval", fake.comments[-1][1])
        self.assertTrue(any("escalated" in a for a in actions))

        fake.label_calls.clear()                                             # later passes: no builder, no repeat
        work["labels"] = [{"name": "status:blocked"}, {"name": "needs:director"}]
        work["comments"] = [{"createdAt": "2026-10-09T10:40:00Z", "body": fake.comments[-1][1]}]
        detail["comments"].append(changes("2026-10-09T10:45:00Z"))         # even a third CHANGES on the PR
        d.housekeeping()
        self.assertEqual(fake.label_calls, [])

        work["labels"] = [{"name": "status:review"}]                         # the director sent it back with a new bar:
        d.housekeeping()                                                     # rounds count afresh from the escalation
        self.assertEqual(fake.label_calls, [to_builder])
    def test_review_skip_prs_merge_whatever_the_branch(self):
        """commonroom#22: green review:skip audit PRs on audit-* branches sat open for 8-10 hours."""
        green = {"commits": [], "comments": [], "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        approved = {**green, "comments": [{"createdAt": "2026-10-09T10:00:00Z", "body": "REVIEW: APPROVE"}]}
        cancelled = {**green, "statusCheckRollup": [{"name": "Fast tests", "conclusion": "CANCELLED",
                                                     "detailsUrl": "https://github.com/o/t/actions/runs/33/job/1"}]}
        skip = [{"name": "review:skip"}]
        prs = [{"number": 244, "headRefName": "audit-20261007-1200", "labels": skip},
               {"number": 245, "headRefName": "director-plan", "labels": []},          # not promised: its author lands it
               {"number": 246, "headRefName": "moonshot/swarm", "labels": skip},
               {"number": 247, "headRefName": "audit-20261007-1300", "labels": skip}]
        details = {244: green, 245: approved, 246: cancelled, 247: {**green, "mergeable": "CONFLICTING"}}
        fake = FakeBoard([issue(1, ["status:ready"])], prs=prs, details=details)
        fake.merges, fake.reruns = [], []
        fake.merge = fake.merges.append
        fake.rerun_ci = lambda run_id, minutes_since: fake.reruns.append(run_id) or 1
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertEqual(fake.merges, [244])
        self.assertIn("merged PR #244 (audit-20261007-1200)", actions)
        self.assertEqual(fake.reruns, ["33"])                    # a cancelled run is still rerun
        self.assertEqual(fake.label_calls, [])                   # no issue to relabel for any of them
        merge = next(r for r in self.ledger.recent_runs("t") if r["role"] == "merge")
        self.assertEqual(merge["task"], "PR #244")               # counts as progress like any merge


class MergesMainOnly(unittest.TestCase):
    """commonroom#14: an approval survives commits that only merge main in. Checked against real git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("core.autocrlf", "false")):
            self.git("config", key, value)
        self.write("a.txt", "one\ntwo\nthree\nfour\nfive\n")
        self.write("b.txt", "bee\n")
        self.commit("base")
        self.git("checkout", "-q", "-b", "feature")
        self.write("a.txt", "ONE\ntwo\nthree\nfour\nfive\n")
        self.approved = self.commit("feature work")   # the reviewed head
        self.git("checkout", "-q", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        import subprocess
        done = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        return done.stdout.strip()

    def write(self, name, text):
        (self.root / name).write_bytes(text.encode())

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge_main_into_feature(self):
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-edit", "main")

    def test_clean_merge_of_main_keeps_the_approval_and_extra_changes_lose_it(self):
        self.write("b.txt", "bee, changed on main\n")
        self.commit("main moves")
        self.merge_main_into_feature()
        self.assertTrue(dispatch.merges_main_only(self.root, self.git("rev-parse", "HEAD"), main="main"))
        self.write("b.txt", "bee, sneaked into the merge\n")                 # an "evil merge"
        self.git("commit", "-q", "-a", "--amend", "--no-edit")
        self.assertFalse(dispatch.merges_main_only(self.root, self.git("rev-parse", "HEAD"), main="main"))
        self.assertFalse(dispatch.merges_main_only(self.root, self.approved, main="main"))   # not a merge at all

    def test_conflict_resolution_keeps_it_only_inside_the_conflict(self):
        self.write("a.txt", "uno\ntwo\nthree\nfour\nfive\n")                  # main edits the line feature edited
        self.commit("main renames one")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "main")
        self.assertEqual(self.git("diff", "--name-only", "--diff-filter=U"), "a.txt")   # a real conflict
        self.write("a.txt", "ONE (uno)\ntwo\nthree\nfour\nfive\n")
        resolved = self.commit("Merge main into feature")
        self.assertTrue(dispatch.merges_main_only(self.root, resolved, main="main"))
        self.write("a.txt", "ONE (uno)\ntwo\nthree\nfour\nFIVE, while I was here\n")
        self.git("commit", "-q", "-a", "--amend", "--no-edit")
        self.assertFalse(dispatch.merges_main_only(self.root, self.git("rev-parse", "HEAD"), main="main"))

    def test_merging_another_branch_is_not_merging_main(self):
        self.git("checkout", "-q", "-b", "side", "main")
        self.write("c.txt", "side\n")
        self.commit("side work")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-edit", "side")
        self.assertFalse(dispatch.merges_main_only(self.root, self.git("rev-parse", "HEAD"), main="main"))

    def test_resolution_text_check(self):
        conflicted = "a\n<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\nb\n<<<<<<< ours\np\n=======\nq\n>>>>>>> theirs\nc\n"
        self.assertTrue(dispatch.resolves_only_conflicts(conflicted, "a\nxy\nb\nq\nc\n"))
        self.assertTrue(dispatch.resolves_only_conflicts(conflicted, "a\nb\nc\n"))
        self.assertFalse(dispatch.resolves_only_conflicts(conflicted, "a\nxy\nB\nq\nc\n"))
        self.assertFalse(dispatch.resolves_only_conflicts(conflicted, "a\nb\nc\nextra\n"))
        self.assertFalse(dispatch.resolves_only_conflicts("same\n", "changed\n"))


class WorktreeLifecycle(unittest.TestCase):
    """Real git: a main checkout with the room's worktrees under .claude/worktrees, as on the owner's PC."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")
        self.root = Path(self.tmp.name) / "game"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            self.git("config", k, v)
        (self.root / "a.txt").write_text("a")
        self.git("add", "-A"); self.git("commit", "-qm", "base")
        self.lines = []
        self.board = FakeBoard([issue(5, ["status:in-progress", "role:builder"])])
        self.board.comments = []
        self.board.comment = lambda n, body: self.board.comments.append((n, body))
        self.d = dispatch.Dispatcher(cfg_with(self.root), self.ledger, board=self.board, log=self.lines.append)
        self.base = self.root / ".claude" / "worktrees"

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return dispatch.git(list(args), cwd or self.root)

    def test_a_leftover_worktree_never_blocks_the_next_checkout(self):
        # commonroom#9: a failed reviewer left review-124 behind and five retries died on "already exists".
        # Fixed in 2351bb3 (2026-10-06 11:19); this pins it.
        path = self.base / "review-124"
        self.git("worktree", "add", "-q", "--detach", str(path), "main")
        self.assertEqual(self.d.checkout(path, "main", None, False), path)       # removed and made again in place
        held = open(path / "held.txt", "w")                                       # a test the last run left running
        try:
            fresh = self.d.checkout(path, "main", None, False)
        finally:
            held.close()
        self.assertTrue((fresh / ".git").exists())
        if os.name == "nt":
            self.assertNotEqual(fresh, path)                                      # Windows can't delete a held file

    def test_git_from_a_gutted_worktree_fails_instead_of_using_the_main_checkout(self):
        # commonroom#17: the auditor's worktree lost its .git link mid-run; git there then answered as the main checkout.
        wt = self.base / "i5-x"
        self.git("worktree", "add", "-q", "-b", "i5-x", str(wt), "main")
        (wt / ".git").unlink()
        (wt / "work.txt").write_text("unsaved")
        with self.assertRaises(RuntimeError):
            self.git("rev-parse", "--show-toplevel", cwd=wt)
        head = self.git("rev-parse", "HEAD")
        self.d.preserve_work(dispatch.Job("build", "builder", issue=self.board.issues[0]), wt, "i5-x")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)                     # nothing committed in the owner's checkout
        self.assertEqual(self.git("status", "--porcelain", "--", "a.txt"), "")

    def test_workers_cannot_reach_the_main_checkout_either(self):
        seen = {}
        def fake_popen(cmd, **kwargs):
            seen.update(kwargs["env"]); raise RuntimeError("not starting a real worker")
        wt = self.base / "auditor-1"
        wt.mkdir(parents=True)
        orig = workers.subprocess.Popen
        workers.subprocess.Popen = fake_popen
        try:
            with self.assertRaises(RuntimeError):
                workers.run_worker("codex", workers.Role("auditor", None, [], "x"), "y", wt, self.ledger, Path(self.tmp.name) / "logs")
        finally:
            workers.subprocess.Popen = orig
            import shutil; shutil.rmtree(workers.worker_tmp(wt), ignore_errors=True)
        self.assertEqual(seen["GIT_CEILING_DIRECTORIES"], str(self.base.resolve()))

    def test_a_live_runs_worktree_is_never_removed_and_removals_are_logged(self):
        wt = self.base / "i5-x"
        self.git("worktree", "add", "-q", "-b", "i5-x", str(wt), "main")
        live = self.ledger.start_run("t", "#5", "builder", "claude")
        self.ledger.set_worktree(live, wt)
        orphan = self.ledger.start_run("t", "#5", "builder", "claude", now=time.time() - 3 * 3600)   # its dispatcher died
        self.d.cleanup_worktree(wt, "someone-else")
        self.assertTrue((wt / ".git").exists())
        self.assertTrue(any(f"run {live}" in line and "still working" in line for line in self.lines))
        self.assertEqual(len(self.d.recover_orphans()), 1)                        # the orphan is closed...
        self.assertTrue((wt / ".git").exists())                                   # ...but the live run's copy stays
        self.assertEqual({r["id"] for r in self.ledger.running_runs("t")}, {live})
        self.ledger.finish_run(live, "done")
        self.d.cleanup_worktree(wt, live)
        self.assertFalse(wt.exists())
        self.assertTrue(any(line.strip() == f"removing worktree {wt} (run {live})" for line in self.lines))
        gone = self.base / "auditor-2"
        self.git("worktree", "add", "-q", "--detach", str(gone), "main")
        import shutil; shutil.rmtree(gone)                                          # deleted by someone outside the room
        self.d.cleanup_worktree(self.base / "unrelated", "run-b")
        self.assertTrue(any("prune (run run-b)" in line and "auditor-2" in line for line in self.lines))
        self.assertIsNotNone(orphan)

    def test_the_audit_report_path_is_absolute_and_picked_up(self):
        self.d._worktree = self.base / "auditor-3"
        job = dispatch.Job("audit", "auditor", reason="periodic audit")
        report = self.d.report_path()
        self.assertTrue(report.is_absolute())
        self.assertIn(f"`{report}`", self.d.prompt(job, None))
        report.parent.mkdir(parents=True)
        report.write_text("all quiet", encoding="utf-8")
        status, outputs, _ = self.d.assess(job, None, None, "", workers.WorkerResult(ok=True, final_text="ACTIONS: []"))
        self.assertEqual((status, outputs[0]["kind"]), ("done", "report"))
        self.assertEqual((self.d.reports_dir() / "latest.md").read_text(encoding="utf-8"), "all quiet")


class StackedMerge(unittest.TestCase):
    def test_merge_retargets_stacked_prs_before_deleting_the_branch(self):
        calls = []

        def fake_gh(args, timeout=30):
            calls.append(args)
            if args[:2] == ["pr", "view"]:
                return {"headRefName": "i277-base", "baseRefName": "main"}
            if args[:2] == ["pr", "list"]:
                return [{"number": 368}]
            return None

        orig = dispatch.board_mod.gh
        dispatch.board_mod.gh = fake_gh
        try:
            dispatch.GitHubBoard("o/t").merge(366)
        finally:
            dispatch.board_mod.gh = orig
        merge = next(i for i, c in enumerate(calls) if c[:2] == ["pr", "merge"])
        retarget = next(i for i, c in enumerate(calls) if c[:3] == ["pr", "edit", "368"])
        delete = next(i for i, c in enumerate(calls) if c[:3] == ["api", "-X", "DELETE"])
        self.assertNotIn("--delete-branch", calls[merge])                    # GitHub would close #368 instead
        self.assertEqual(calls[retarget][-2:], ["--base", "main"])
        self.assertLess(retarget, delete)
        self.assertTrue(calls[delete][-1].endswith("/git/refs/heads/i277-base"))

    def test_squash_commit_reads_as_the_pr_not_the_branch_commits(self):
        """commonroom#21: a branch whose only commit was the room's WIP placeholder put that text on main."""
        calls = []
        body = "Closes #229\r\n\r\nImport until the global class cache exists.\r\n" + "x" * 5000 + "\r\nfooter"

        def fake_gh(args, timeout=30):
            calls.append(args)
            if args[:2] == ["pr", "view"]:
                return {"headRefName": "i229-export", "baseRefName": "main",
                        "title": "Android export: import until the global class cache exists (#229)", "body": body}
            return [] if args[:2] == ["pr", "list"] else None

        orig = dispatch.board_mod.gh
        dispatch.board_mod.gh = fake_gh
        try:
            dispatch.GitHubBoard("o/t").merge(259)
        finally:
            dispatch.board_mod.gh = orig
        merge = next(c for c in calls if c[:2] == ["pr", "merge"])
        self.assertIn("--subject=Android export: import until the global class cache exists (#229) (#259)", merge)
        sent = next(a for a in merge if a.startswith("--body="))[len("--body="):]
        self.assertTrue(sent.startswith("Closes #229\n\nImport until"))           # the PR's words, LF only
        self.assertNotIn("\r", sent)
        self.assertLess(len(sent), dispatch.SQUASH_BODY_MAX + 100)                 # trimmed at a line break
        self.assertTrue(sent.endswith("(Trimmed: the full description is on PR #259.)"))
        self.assertNotIn("WIP", " ".join(merge))
        # A title that already names the PR isn't suffixed twice; an empty body still overrides the commit list.
        self.assertEqual(dispatch.squash_message(7, "Fix it (#7)", None), ["--subject=Fix it (#7)", "--body="])


if __name__ == "__main__":
    unittest.main()
