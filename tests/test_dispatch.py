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

    def test_director_cooldown(self):
        issues = [issue(30, ["needs:director"])]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIn("escalated", d.director_trigger())
        run = self.ledger.start_run("t", None, "director", "claude")
        self.ledger.finish_run(run, "done")
        self.assertIsNone(d.director_trigger())


if __name__ == "__main__":
    unittest.main()
