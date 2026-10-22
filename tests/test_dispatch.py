import contextlib
import datetime
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    queued, cancelled, queued_error = [], [], None

    def queued_runs(self):
        if self.queued_error:
            raise RuntimeError(self.queued_error)
        self.listed = True
        return self.queued

    def cancel_run(self, run_id):
        self.cancelled = self.cancelled + [run_id]

    finished = []

    def finished_prs(self):
        return [{"number": n} for n in self.finished]

    def pr_detail(self, n):
        return self.details[n]

    def set_labels(self, n, add=(), remove=()):
        self.label_calls.append((n, tuple(add), tuple(remove)))

    def merge(self, n):
        self.merged = n

    def comment(self, n, body):
        self.comments = getattr(self, "comments", []) + [(n, body)]

    def set_milestone(self, n, title):
        self.milestones = getattr(self, "milestones", []) + [(n, title)]
        for i in self.issues:
            if i["number"] == n:
                i["milestone"] = {"title": title}

    def close_issue(self, n, body):
        self.closed = getattr(self, "closed", []) + [(n, body)]

    def close_pr(self, n, body):
        self.closed_prs = getattr(self, "closed_prs", []) + [(n, body)]

    # Merge safety (commonroom#19): by default every branch contains main and main's CI isn't red.
    behind, main_runs = {}, []

    def behind_main(self, detail):
        return self.behind.get(detail.get("number"), 0)

    def update_branch(self, n):
        self.updated = getattr(self, "updated", []) + [n]

    def main_checks(self):
        return self.main_runs

    def rerun_failed(self, run_id):
        self.reruns = getattr(self, "reruns", []) + [run_id]

    def create_issue(self, title, body, labels=(), milestone=None, assignees=()):
        self.created = getattr(self, "created", []) + [(title, tuple(labels), milestone)]
        self.created_bodies = getattr(self, "created_bodies", []) + [body]
        self.created_assignees = getattr(self, "created_assignees", []) + [tuple(assignees)]
        return 900

    def ensure_label(self, name):
        self.ensured = getattr(self, "ensured", []) + [name]

    def edit_issue(self, n, title, body):
        self.edited = getattr(self, "edited", []) + [(n, title, body)]

    def issue_comments(self, n, since):
        self.comment_reads = getattr(self, "comment_reads", []) + [n]
        return next((i["comments"] for i in self.issues if i["number"] == n), [])


class LabelBoard(FakeBoard):
    """A FakeBoard whose label edits land on its issues, so a test can check the labels an issue ends with."""

    def set_labels(self, n, add=(), remove=()):
        super().set_labels(n, add, remove)
        for i in self.issues:
            if i["number"] == n:
                kept = [l for l in i["labels"] if l["name"] not in remove]
                i["labels"] = kept + [{"name": l} for l in add if l not in {k["name"] for k in kept}]


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

    def test_only_a_comment_that_starts_with_a_verdict_is_one(self):
        """commonroom#58: a builder quoting `REVIEW: CHANGES` in its reply escalated #49 as a second round."""
        for body, verdict in (("REVIEW: APPROVE\nLedger rows checked: none relevant", "APPROVE"),   # gamemirage's reviewer
                              ("  \n[agent:reviewer] REVIEW: CHANGES - fix x", "CHANGES"),
                              ("[agent:director]REVIEW: APPROVE", "APPROVE"),
                              ("[agent:builder] A `REVIEW: CHANGES` now sends it back.", None),
                              ("[agent:builder] REVIEW: APPROVE", None),
                              ("[room] REVIEW: CHANGES", None),
                              ("Fixed. REVIEW: APPROVE please", None),
                              ("[agent:reviewer] Looks fine.\nREVIEW: APPROVE", None),
                              ("", None), (None, None)):
            self.assertEqual(dispatch.verdict_of(body), verdict, body)

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

    def test_housekeeping_unblocks_on_merged_pr_not_open_pr(self):
        # gamemirage#522 depended on #517, a merged PR, and sat blocked 10 h (commonroom#108).
        issues = [issue(2, ["status:blocked"], body="Depends on #517"), issue(3, ["status:blocked"], body="Depends on #518"),
                  issue(4, ["status:blocked"], body="Depends on #517 and #518")]
        fake = FakeBoard(issues, prs=[{"number": 518, "headRefName": "other"}])
        fake.finished = [517]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        d.housekeeping()
        self.assertEqual({call[0] for call in fake.label_calls if "status:ready" in call[1]}, {2})

    def playtest_pass(self, cfg, issues, n, files):
        """One housekeeping pass that merges PR #n, which changes `files`."""
        detail = {"number": n, "headRefName": f"i{n}-x", "files": [{"path": f} for f in files],
                  "commits": [{"committedDate": "2026-10-05T12:00:00Z"}],
                  "comments": [{"createdAt": "2026-10-05T13:00:00Z", "body": "REVIEW: APPROVE"}],
                  "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        fake = FakeBoard(issues, prs=[{"number": n, "headRefName": f"i{n}-x", "title": f"Change {n}"}], details={n: detail})
        actions = dispatch.Dispatcher(cfg, self.ledger, board=fake, log=lambda *_: None).housekeeping()
        self.assertEqual(fake.merged, n)
        return fake, actions

    def test_merges_touching_playtest_paths_open_then_extend_one_playtest_issue(self):
        """commonroom#53: visual PRs landed unchecked by eye and broken visuals were found hours later."""
        cfg = cfg_with(self.tmp.name)
        cfg["playtester"] = {"paths": ["game/view/**", "art/**"]}
        issues = []
        fake, actions = self.playtest_pass(cfg, issues, 7, ["game/view/hud.gd", "README.md"])
        self.assertEqual(fake.created, [("Playtest after merges: #7", ("role:playtester", "status:ready"), "M0")])
        self.assertIn("- PR #7: Change 7", fake.created_bodies[0])
        self.assertIn("PR #7 touches playtest paths: opened playtest #900", actions)

        # While it's open, the next matching merge joins it instead of opening another.
        fake, actions = self.playtest_pass(cfg, issues, 8, ["art/units/tank.png"])
        self.assertFalse(hasattr(fake, "created"))
        (n, title, body), = fake.edited
        self.assertEqual((n, title), (900, "Playtest after merges: #7, #8"))
        self.assertIn("- PR #7: Change 7\n- PR #8: Change 8\n", body)

        # A merge touching no playtest path does nothing.
        fake, actions = self.playtest_pass(cfg, issues, 9, ["docs/notes.md", "game/viewer.gd"])
        self.assertFalse(hasattr(fake, "created") or hasattr(fake, "edited"))
        self.assertFalse(any("playtest" in a for a in actions))

    def test_no_playtest_opens_within_every_hours_of_the_last_one_closing(self):
        cfg = cfg_with(self.tmp.name)
        cfg["playtester"] = {"paths": ["game/view/**"], "every_hours": 6}
        stamp = lambda hours_ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours_ago * 3600))
        last = issue(50, ["role:playtester"], state="CLOSED") | {"title": "Playtest after merges: #40", "closedAt": stamp(2)}
        fake, actions = self.playtest_pass(cfg, [last], 7, ["game/view/hud.gd"])
        self.assertFalse(hasattr(fake, "created") or hasattr(fake, "edited"))
        self.assertTrue(any("under [playtester] every_hours (6)" in a for a in actions))

        last["closedAt"] = stamp(7)
        fake, _ = self.playtest_pass(cfg, [last], 7, ["game/view/hud.gd"])
        self.assertEqual(fake.created[0][0], "Playtest after merges: #7")

        # The default gap is 6 h too.
        del cfg["playtester"]["every_hours"]
        last["closedAt"] = stamp(5)
        fake, _ = self.playtest_pass(cfg, [last], 7, ["game/view/hud.gd"])
        self.assertFalse(hasattr(fake, "created"))

    def test_without_a_playtester_table_merges_never_open_a_playtest(self):
        # commonroom, and gamemirage until its director configures one: behaviour is unchanged.
        self.assertNotIn("playtester", config.load(Path(__file__).resolve().parents[1] / "room.toml", follow_host=False))
        last = issue(50, ["role:playtester"]) | {"title": "Playtest after merges: #40"}
        for issues in ([], [last]):
            fake, actions = self.playtest_pass(cfg_with(self.tmp.name), issues, 7,
                                               ["game/view/hud.gd", "art/x.png", "commonroom/dispatch.py"])
            self.assertFalse(hasattr(fake, "created") or hasattr(fake, "edited"))
            self.assertFalse(any("playtest" in a for a in actions))

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

    def top_pass(self, issues, dry_run=False):
        fake = FakeBoard(issues)
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None, dry_run=dry_run)
        return fake, d.housekeeping()

    def test_prio_top_with_no_status_is_marked_ready(self):
        """commonroom#92: gamemirage#444 was raised to prio:top without a status and no dispatcher could see it."""
        fake, actions = self.top_pass([issue(44, ["prio:top", "role:builder"])])
        self.assertEqual(fake.label_calls, [(44, ("status:ready",), ())])
        self.assertEqual(fake.comments, [(44, "[room] #44 is prio:top but had no status label, so nothing could pick "
                                              "it up. Marked status:ready.")])
        self.assertEqual([a for a in actions if "prio:top" in a], ["#44 is prio:top with no status: marked ready"])

    def test_prio_top_with_no_status_waits_for_an_open_dependency(self):
        fake, actions = self.top_pass([issue(44, ["prio:top", "role:builder"], body="Depends on #40"), issue(40, [])])
        self.assertEqual(fake.label_calls, [(44, ("status:blocked",), ())])
        self.assertEqual(fake.comments, [(44, "[room] #44 is prio:top but had no status label, so nothing could pick "
                                              "it up. Marked status:blocked.")])
        self.assertIn("#44 is prio:top with no status: marked blocked", actions)
        fake, _ = self.top_pass([issue(44, ["prio:top", "role:builder"], body="Depends on #40"),
                                 issue(40, [], state="CLOSED")])
        self.assertEqual(fake.label_calls, [(44, ("status:ready",), ())])

    def test_other_issues_without_status_are_left_alone(self):
        issues = [issue(1, ["prio:top", "role:builder", "status:ready"]), issue(2, ["prio:top", "role:builder", "status:blocked"]),
                  issue(3, ["prio:top", "role:builder", "owner:human"]), issue(4, ["prio:top", "role:builder", "needs:director"]),
                  issue(5, ["prio:top", "role:builder", "parked"]), issue(6, ["prio:top"]),
                  issue(7, ["prio:top", "role:builder"], milestone="Other"), issue(8, ["prio:top", "role:builder"], milestone=None),
                  issue(9, ["prio:high", "role:builder"]), issue(10, ["prio:top", "role:builder"], state="CLOSED")]
        fake, actions = self.top_pass(issues)
        self.assertEqual(fake.label_calls, [])
        self.assertFalse(hasattr(fake, "comments"))
        self.assertFalse(any("prio:top" in a for a in actions))

    def test_prio_top_with_no_status_in_dry_run_is_only_reported(self):
        fake, actions = self.top_pass([issue(44, ["prio:top", "role:builder"])], dry_run=True)
        self.assertIn("#44 is prio:top with no status: marked ready", actions)
        self.assertEqual(fake.label_calls, [])
        self.assertFalse(hasattr(fake, "comments"))

    def test_ready_issue_with_no_milestone_is_filed_in_the_goal(self):
        """commonroom#100: #93 was ready with no milestone and no dispatcher could see it for 8.5 h."""
        fake = FakeBoard([issue(93, ["status:ready", "role:builder", "prio:high"], milestone=None)])
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertEqual(fake.milestones, [(93, "M0")])
        self.assertEqual(fake.comments, [(93, "[room] #93 was ready with no milestone, so nothing could pick it up. "
                                              "Filed it in M0.")])
        self.assertEqual([a for a in actions if "milestone" in a], ["#93 was ready with no milestone: filed in M0"])
        self.assertEqual([i["number"] for i in fake.open_issues("M0")], [93])
        actions = d.housekeeping()
        self.assertEqual(fake.milestones, [(93, "M0")])
        self.assertEqual(len(fake.comments), 1)
        self.assertFalse(any("milestone" in a for a in actions))

    def test_other_issues_keep_their_milestone(self):
        issues = [issue(1, ["status:ready", "role:builder"], milestone="Other"), issue(2, ["role:builder"], milestone=None),
                  issue(3, ["status:blocked", "role:builder"], milestone=None), issue(4, ["status:ready"], milestone=None),
                  issue(5, ["status:ready", "role:builder", "parked"], milestone=None),
                  issue(6, ["status:ready", "role:builder", "owner:human"], milestone=None),
                  issue(7, ["status:ready", "role:builder", "needs:director"], milestone=None),
                  issue(8, ["status:ready", "role:builder"], milestone=None, state="CLOSED")]
        fake, actions = self.top_pass(issues)
        self.assertFalse(hasattr(fake, "milestones") or hasattr(fake, "comments"))
        self.assertFalse(any("milestone" in a for a in actions))

    def test_no_goal_milestone_files_nothing(self):
        cfg = cfg_with(self.tmp.name)
        cfg["goal"]["milestone"] = None
        fake = FakeBoard([issue(93, ["status:ready", "role:builder"], milestone=None)])
        actions = dispatch.Dispatcher(cfg, self.ledger, board=fake, log=lambda *_: None).housekeeping()
        self.assertFalse(hasattr(fake, "milestones") or hasattr(fake, "comments"))
        self.assertFalse(any("milestone" in a for a in actions))

    def test_ready_issue_with_no_milestone_in_dry_run_is_only_reported(self):
        fake, actions = self.top_pass([issue(93, ["status:ready", "role:builder"], milestone=None)], dry_run=True)
        self.assertIn("#93 was ready with no milestone: filed in M0", actions)
        self.assertFalse(hasattr(fake, "milestones") or hasattr(fake, "comments"))
        self.assertIsNone(fake.issues[0]["milestone"])

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

    def test_escalation_for_the_interactive_session_does_not_wake_the_director(self):
        cfg = cfg_with(self.tmp.name)
        cfg["director"] = config.DEFAULTS["director"]
        issues = [issue(30, ["status:ready", "role:builder"]),
                  issue(140, ["needs:director"], body="[agent:director]\nInteractive session: edit the live script", milestone=None)]
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        now = time.time()
        last = self.ledger.start_run("t", None, "director", "claude", now=now - 2 * 3600)
        self.ledger.finish_run(last, "done", outputs=[{"kind": "director", "ref": "x"}], now=now - 2 * 3600 + 60)
        self.assertIsNone(d.director_trigger())                        # no steer due: no wake
        self.ledger.db.execute("UPDATE runs SET started_at=? WHERE id=?", (now - 3 * 3600 - 60, last)); self.ledger.db.commit()
        trigger = d.director_trigger()                                 # steer due: the issue is named
        self.assertIn(dispatch.STEER_REASON, trigger)
        self.assertIn("#140 waits for the interactive session", trigger)
        self.assertNotIn("escalated", trigger)
        issues.append(issue(141, ["needs:director"], body="Please decide", milestone=None))
        self.assertIn("#141 escalated to the director", d.director_trigger())

    def test_nothing_ready_counts_only_the_goal_milestone(self):
        issues = [issue(31, [], milestone="Later"), issue(32, [], milestone=None)]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        self.assertIsNone(d.director_trigger())                        # unlabelled work outside the goal: no wake
        issues[:] = [issue(30, ["role:builder"]), issue(33, ["status:ready", "role:builder"], milestone="Later")]
        self.assertIn("nothing is ready", d.director_trigger())        # ready work elsewhere doesn't hide the goal's

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

    def bulletin_setup(self, bulletin=True):
        cfg = cfg_with(self.tmp.name)
        if bulletin:
            cfg["bulletin"] = {"issue": 286, "deadlines": ["10:00", "21:00"]}
        digest = issue(286, [], milestone=None)
        board = FakeBoard([issue(30, ["status:ready", "role:builder"]), digest])
        return dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None), board, digest

    @staticmethod
    def local(day, hour, minute=0):
        return datetime.datetime(2026, 10, day, hour, minute).timestamp()

    @staticmethod
    def said(body, stamp):
        return {"body": body, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))}

    def test_bulletin_window_is_the_latest_passed_deadline_and_the_one_before(self):
        self.assertEqual(dispatch.bulletin_window(["10:00", "21:00"], self.local(11, 12)),
                         (self.local(10, 21), self.local(11, 10), "10:00"))
        self.assertEqual(dispatch.bulletin_window(["21:00", "10:00"], self.local(11, 9)),     # before today's first
                         (self.local(10, 10), self.local(10, 21), "21:00"))
        self.assertEqual(dispatch.bulletin_window(["9:30"], self.local(11, 9, 30)),           # a single daily slot
                         (self.local(10, 9, 30), self.local(11, 9, 30), "09:30"))

    def test_missed_bulletin_wakes_the_director(self):
        """commonroom#82: the session crons that post the owner's digest were wiped silently."""
        d, board, digest = self.bulletin_setup()
        with mock.patch.object(dispatch.time, "time", return_value=self.local(11, 12)):
            reason = d.director_trigger()
            self.assertEqual(reason, dispatch.BULLETIN_REASON.format(due="10:00", issue=286, owner="o"))
            self.assertIn("bulletin missed (due 10:00): post a short text digest to #286 starting with @o", reason)
            # Agent comments don't count, nor does the bulletin for the previous slot (posted before its deadline).
            digest["comments"] += [self.said("[agent:director] @o morning digest", self.local(11, 8)),
                                   self.said("@o evening digest", self.local(10, 19, 19))]
            self.assertIn("bulletin missed (due 10:00)", d.director_trigger())
            self.assertEqual(board.comment_reads, [286, 286])

    def test_bulletin_posted_early_counts(self):
        d, _, digest = self.bulletin_setup()
        digest["comments"].append(self.said("@o morning digest: 3 merges", self.local(11, 8, 14)))   # before the 10:00 slot
        with mock.patch.object(dispatch.time, "time", return_value=self.local(11, 12)):
            self.assertIsNone(d.director_trigger())
        digest["comments"][-1] = self.said("  @o late digest", self.local(11, 11))
        with mock.patch.object(dispatch.time, "time", return_value=self.local(11, 12)):
            self.assertIsNone(d.director_trigger())

    def test_bulletin_fires_once_per_deadline(self):
        d, _, digest = self.bulletin_setup()
        run = self.ledger.start_run("t", None, "director", "claude", now=self.local(11, 10, 30))
        self.ledger.finish_run(run, "done", outputs=[{"kind": "director", "ref": "x"}], now=self.local(11, 10, 40))
        with mock.patch.object(dispatch.time, "time", return_value=self.local(11, 12)):
            self.assertIsNone(d.director_trigger())        # that run was given the reason; no second wake for 10:00
        digest["comments"].append(self.said("@o morning digest", self.local(11, 10, 35)))
        with mock.patch.object(dispatch.time, "time", return_value=self.local(11, 22)):
            self.assertIsNone(d.director_trigger())        # it posted after 10:00, which counts for 21:00
        digest["comments"].clear()
        with mock.patch.object(dispatch.time, "time", return_value=self.local(11, 22)):
            self.assertIn("bulletin missed (due 21:00)", d.director_trigger())

    def test_no_bulletin_table_leaves_the_reasons_unchanged(self):
        # gamemirage today: no [bulletin], so no comment read and the same reasons as before.
        d, board, _ = self.bulletin_setup(bulletin=False)
        with mock.patch.object(dispatch.time, "time", return_value=self.local(11, 12)):
            self.assertIsNone(d.director_trigger())
            board.issues[0] = issue(30, ["role:builder"])
            self.assertEqual(d.director_trigger(),
                             "the milestone has open work but nothing is ready: unblock, re-plan or split tasks")
        self.assertFalse(hasattr(board, "comment_reads"))
        self.assertEqual(config.DEFAULTS["bulletin"], {})

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

    def audited(self, every_hours=1, quiet_paths=None, hours_ago=2):
        """A dispatcher with `[audit]` on, and an audit that started `hours_ago` and ran for 10 minutes."""
        cfg = cfg_with(self.tmp.name)
        cfg["audit"] = {"every_hours": every_hours, **({"quiet_paths": quiet_paths} if quiet_paths else {})}
        now = time.time()
        a = self.ledger.start_run("t", None, "auditor", "claude", now=now - hours_ago * 3600)
        self.ledger.finish_run(a, "done", outputs=[{"kind": "report", "ref": "x"}], now=now - hours_ago * 3600 + 600)
        return cfg, now - hours_ago * 3600 + 300

    def merge_one(self, cfg, number, branch, created, files, labels=()):
        """Housekeeping merges one green PR; returns its merge run's output."""
        detail = {"number": number, "headRefName": branch, "commits": [], "comments": [], "createdAt": created,
                  "statusCheckRollup": [{"conclusion": "SUCCESS"}], "files": [{"path": f} for f in files],
                  "comments": [{"createdAt": "2026-10-12T03:00:00Z", "body": "[agent:reviewer] REVIEW: APPROVE"}]}
        fake = FakeBoard([issue(1, ["status:review"])], details={number: detail},
                         prs=[{"number": number, "headRefName": branch, "labels": [{"name": l} for l in labels]}])
        fake.merges = []
        fake.merge = fake.merges.append
        dispatch.Dispatcher(cfg, self.ledger, board=fake, log=lambda *_: None).housekeeping()
        self.assertEqual(fake.merges, [number])
        return next(r for r in self.ledger.recent_runs("t") if r["role"] == "merge")["outputs"][0]

    @staticmethod
    def iso(ts):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

    def age_runs(self, hours):
        self.ledger.db.execute("UPDATE runs SET started_at=started_at-?, ended_at=ended_at-?", (hours * 3600, hours * 3600))
        self.ledger.db.commit()

    def test_merging_the_audits_own_pr_doesnt_make_the_next_audit_due(self):
        """commonroom#103: merging audit PR #521 made the next audit due, and audit PR #538 re-captured the same build."""
        cfg, during = self.audited()
        out = self.merge_one(cfg, 521, "audit-2026-10-11-2145", self.iso(during), ["docs/captures/a/sheet.png"],
                             labels=["review:skip"])
        self.assertEqual(out["quiet"], "auditor")
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.assertIsNone(d.audit_due())                       # 2 h on, every_hours=1, but only its own merge since
        self.age_runs(3.5)
        self.assertIsNone(d.audit_due())                       # 5.5 h on: still before the floor
        self.age_runs(0.6)
        self.assertIn("periodic audit", d.audit_due())         # the 6 h floor stands

    def test_a_builder_run_after_the_audit_makes_it_due_at_every_hours(self):
        cfg, _ = self.audited(every_hours=2, hours_ago=1)
        run = self.ledger.start_run("t", "#3", "builder", "claude"); self.ledger.finish_run(run, "done")
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.assertIsNone(d.audit_due())                       # 1 h on: not yet
        self.age_runs(1)
        self.assertIn("periodic audit", d.audit_due())         # 2 h on: due

    def test_ordinary_merges_still_count_and_docs_only_merges_dont_when_configured(self):
        # An issue branch opened during the audit is a builder's PR, not the auditor's: it counts, as before (and as
        # it does for gamemirage, which sets no quiet_paths).
        cfg, during = self.audited()
        out = self.merge_one(cfg, 600, "i1-thing", self.iso(during), ["docs/notes.md"])
        self.assertNotIn("quiet", out)
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.assertIn("periodic audit", d.audit_due())

        self.ledger.db.execute("DELETE FROM runs"); self.ledger.db.commit()
        cfg, _ = self.audited(quiet_paths=["docs/**", "design/decisions/**"])
        out = self.merge_one(cfg, 601, "i1-thing", "2026-10-01T00:00:00Z", ["docs/a/notes.md", "design/decisions/d1.md"])
        self.assertEqual(out["quiet"], "docs")
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.assertIsNone(d.audit_due())                       # docs only: nothing for an audit to look at

        self.ledger.db.execute("DELETE FROM runs"); self.ledger.db.commit()
        cfg, _ = self.audited(quiet_paths=["docs/**"])
        out = self.merge_one(cfg, 602, "i1-thing", "2026-10-01T00:00:00Z", ["docs/notes.md", "src/game.py"])
        self.assertNotIn("quiet", out)                         # one code file: it counts
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.assertIn("periodic audit", d.audit_due())

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

    def blocked_build(self, provider, labels, changed):
        """One builder run on #84 that ends `STATUS: BLOCKED`, in a real git worktree with or without changes."""
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(builder=["claude:sonnet", "codex"], builder_heavy=["claude:opus"])
        work = issue(84, ["status:in-progress", "role:builder", "size:S", *labels])
        board = FakeBoard([work])
        board.pr_for_branch = lambda branch: None
        board.ensured = []
        board.ensure_label = board.ensured.append
        d = dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None)
        wt = Path(self.tmp.name) / f"wt-{provider}-{len(labels)}-{changed}"
        wt.mkdir()
        dispatch.git(["init", "-q"], wt)
        if changed:
            (wt / "done.py").write_text("print('finished')\n")
        d._provider, d._worktree = provider, wt
        d.remote_head = lambda branch: None
        report = "Implemented the fix.\n" + "x" * 2000 + "\nPermissionError in tools/test.py, can't run the gate.\nSTATUS: BLOCKED"
        job = dispatch.Job("build", "builder", issue=work, reason="ready")
        status, _, _ = d.assess(job, "i84-handoff", None, "", workers.WorkerResult(ok=True, final_text=report))
        self.assertEqual(status, "blocked")   # still counts toward [breakers] task_failures
        added = {l for _, add, _ in board.label_calls for l in add}
        return board, added

    def test_codex_blocked_with_changes_goes_to_a_claude_builder(self):
        """commonroom#84: Codex finished the work but couldn't run the gate in its sandbox."""
        board, added = self.blocked_build("codex", [], changed=True)
        self.assertEqual(board.label_calls, [(84, ("status:ready", "provider:claude"), ("status:in-progress",))])
        self.assertEqual(board.ensured, ["provider:claude"])
        self.assertFalse(added & {"needs:director", "status:blocked"})
        (n, body), = board.comments
        self.assertEqual(n, 84)
        self.assertIn("Its work is preserved on branch `i84-handoff`", body)
        self.assertIn("A Claude builder continues from it", body)
        self.assertIn("PermissionError in tools/test.py", body)
        self.assertNotIn("Implemented the fix.", body)   # only the last ~1500 characters
        # Pinned provider:claude, the next pick is a Claude build, not the parallel Codex one.
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(builder=["claude:sonnet", "codex"], builder_heavy=["claude:opus"])
        issues = [issue(84, ["status:ready", "role:builder", "size:S", "provider:claude"]),
                  issue(85, ["status:ready", "role:builder", "size:S"]), issue(86, ["status:ready", "role:builder", "size:S"])]
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}
        codex = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="codex")
        claude = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="claude")
        job, provider, _ = codex.pick(allowed)
        self.assertNotEqual(job.issue["number"], 84)
        self.assertEqual(provider, "codex")
        issues[:] = issues[:1]
        self.assertIsNone(codex.pick(allowed))
        job, provider, _ = claude.pick(allowed)
        self.assertEqual((job.issue["number"], provider), (84, "claude"))

    def test_codex_blocked_with_no_changes_goes_to_a_claude_builder(self):
        """commonroom#91: Codex lost `gh` auth before it edited anything (gamemirage#418, #419), and the task sat
        `needs:director` until the director requeued it by hand."""
        board, added = self.blocked_build("codex", [], changed=False)
        self.assertEqual(board.label_calls, [(84, ("status:ready", "provider:claude"), ("status:in-progress",))])
        self.assertEqual(board.ensured, ["provider:claude"])
        self.assertFalse(added & {"needs:director", "status:blocked"})
        (n, body), = board.comments
        self.assertEqual(n, 84)
        self.assertIn("left nothing on branch `i84-handoff`", body)
        self.assertIn("A Claude builder starts from the issue", body)
        self.assertNotIn("preserved", body)
        self.assertIn("PermissionError in tools/test.py", body)
        self.assertNotIn("Implemented the fix.", body)   # only the last ~1500 characters

    def test_other_blocked_builds_still_escalate(self):
        escalate = [(84, ("status:blocked", "needs:director"), ("status:in-progress",))]
        for provider, labels, changed in [("codex", ["provider:codex"], True),     # only Codex can do it
                                          ("codex", ["provider:codex"], False),
                                          ("claude", [], True),                    # Claude, e.g. the continuation
                                          ("claude", [], False),
                                          ("claude", ["provider:claude"], True)]:  # so a handoff can't loop
            with self.subTest(provider=provider, labels=labels, changed=changed):
                board, _ = self.blocked_build(provider, labels, changed)
                self.assertEqual(board.label_calls, escalate)
                self.assertFalse(getattr(board, "comments", []))
                self.assertEqual(board.ensured, [])

    def test_conflicting_pr_with_changes_review_never_goes_to_codex(self):
        """commonroom#51: Codex can't fetch or commit, so a CHANGES-reviewed PR that conflicts with main went to it as
        an ordinary build, and it copied main's files into the tree instead (gamemirage#223, #238)."""
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(builder=["codex", "claude:sonnet"], builder_heavy=["claude:opus"])
        work = issue(174, ["status:review"])
        rejected = {"commits": [{"committedDate": "2026-10-07T01:00:00Z"}], "mergeable": "CONFLICTING",
                    "comments": [{"createdAt": "2026-10-07T02:30:00Z", "body": "REVIEW: CHANGES - fix y"}]}
        fake = FakeBoard([work], prs=[{"number": 181, "headRefName": "i174-x"}], details={181: rejected})
        d = dispatch.Dispatcher(cfg, self.ledger, board=fake, log=lambda *_: None)
        d.housekeeping()
        note = dict(fake.comments)[174]
        self.assertNotIn("merge-main run", note)                          # a full run: it also addresses the review
        work["comments"] = [{"body": "[room] " + note}]
        self.assertTrue(dispatch.conflicts_main(work))
        self.assertEqual(d.build_spec(work), ["claude:sonnet"])          # 1. a Claude choice, though Codex comes first
        work["labels"] = [{"name": n} for n in ("status:ready", "role:builder", "size:S", "provider:codex")]
        self.assertEqual(d.build_spec(work), ["claude:sonnet"])          # ...even over a `provider:codex` pin

        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}
        work["labels"] = [{"name": n} for n in ("status:ready", "role:builder", "size:S")]
        other = issue(175, ["status:ready", "role:builder", "size:S"])
        issues = [other, work]
        codex = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="codex")
        busy = self.ledger.start_run("t", "#99", "builder", "claude")    # Claude busy: Codex may take the top task
        job, provider, _ = codex.pick(allowed)
        self.assertEqual((job.issue["number"], provider), (175, "codex"))  # 4. a non-conflicting issue still goes to Codex
        issues.remove(other)
        self.assertIsNone(codex.pick(allowed))                            # 2. the parallel path and fallback skip it
        work["labels"].append({"name": "provider:codex"})
        self.assertIsNone(codex.pick(allowed))                            # ...and so does the pinned path
        self.ledger.finish_run(busy, "done", outputs=[{"kind": "pr", "ref": "PR #1"}])
        claude = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard(issues), log=lambda *_: None, only_provider="claude")
        job, provider, _ = claude.pick(allowed)
        self.assertEqual((job.issue["number"], provider), (174, "claude"))
        work["comments"].append({"body": "[room] Attempt 1 produced no PR. Back to ready."})
        self.assertFalse(dispatch.conflicts_main(work))                   # the room's latest word decides
        self.assertEqual(d.build_spec(work), ["codex"])

    def test_builder_by_label_routes_builds_commonroom_118(self):
        """`[roles] builder_by_label` sends a kind of work to a provider first; without it nothing changes."""
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(builder=["claude:opus", "codex"], builder_heavy=["claude:opus"])
        sets = [["kind:research"], ["kind:research", "size:L"], ["kind:research", "builder:heavy"],
                ["kind:research", "risk:high"], ["kind:research", "provider:claude"], ["kind:bug"], []]
        base = ["status:ready", "role:builder"]
        plain = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        before = [plain.build_spec(issue(1, base + s)) for s in sets]
        self.assertEqual(before[0], ["claude:opus", "codex"])
        cfg["roles"]["builder_by_label"] = {"kind:research": ["codex", "claude:opus"]}
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        after = [d.build_spec(issue(1, base + s)) for s in sets]
        self.assertEqual(after[0], ["codex", "claude:opus"])
        self.assertEqual(after[1:4], [["claude:opus"]] * 3)                # size:L, builder:heavy, risk:high
        self.assertEqual(after[4], ["claude"])                             # a provider pin still wins
        self.assertEqual(after[5:], before[5:])                            # other labels: as before
        self.assertEqual(dispatch.runnable_roles(cfg), set())

        research = issue(7, base + ["kind:research"])
        claude = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([research]), log=lambda *_: None, only_provider="claude")
        both = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}
        self.assertIsNone(claude.pick(both))                               # Codex is allowed: it keeps the issue
        held = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "held")}
        job, provider, _ = claude.pick(held)
        self.assertEqual((job.issue["number"], provider), (7, "claude"))   # Codex held: Claude takes it

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

    def test_extra_slot_waits_at_max_5h_but_slot_one_starts(self):
        # commonroom#15: gamemirage ran three builders into the 5-hour reserve and two were stopped mid-run.
        cfg = cfg_with(self.tmp.name)
        cfg["roles"].update(builder=["claude:sonnet"])
        self.assertEqual(cfg["parallel"]["max_5h"], 60)                       # default; gamemirage sets none
        issues = [issue(n, ["status:ready", "role:builder"]) for n in (64, 70, 72, 79)]
        board = FakeBoard(issues)
        verdict = lambda five: {"claude": gate.Verdict(True, "ok", {"weekly_pace": 50, "weekly_used": 9, "five_hour_used": five}),
                                "codex": gate.Verdict(False, "dry")}
        slot1 = dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None, only_provider="claude")
        job, provider, _ = slot1.pick(verdict(60))
        self.assertEqual((job.kind, provider), ("build", "claude"))           # slot 1 is unaffected
        self.ledger.start_run("t", "#64", "builder", "claude", exclusive=True)
        slot2 = dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None, only_provider="claude", slot=2)
        self.assertIsNone(slot2.pick(verdict(60)))                             # plenty of weekly headroom, but 60% of 5 h
        job, _, _ = slot2.pick(verdict(59))
        self.assertEqual((job.kind, job.issue["number"]), ("build", 70))
        cfg["parallel"]["max_5h"] = 80
        self.assertIsNotNone(slot2.pick(verdict(60)))                          # the threshold is [parallel] max_5h

    def test_run_row_records_the_5_hour_window_at_launch(self):
        root = Path(self.tmp.name)
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".claude" / "agents" / "builder.md").write_text("---\nmodel: sonnet\n---\nBuild.\n")
        now = time.time()
        self.ledger.record_usage([usage.observation("claude", "five_hour", 61, now + 3600, "test")], now=now)
        d = dispatch.Dispatcher(cfg_with(root), self.ledger, board=FakeBoard([]), log=lambda *_: None)
        def broken(job, provider=None):
            raise RuntimeError("no worktree")
        d.prepare_worktree = broken
        orig = dispatch.git
        dispatch.git = lambda args, cwd: ""
        try:
            with self.assertRaises(RuntimeError):
                d.run_job(dispatch.Job("build", "builder", issue=issue(5, ["status:ready"]), reason="ready"), "claude", "sonnet")
        finally:
            dispatch.git = orig
        row = self.ledger.db.execute("SELECT five_hour_start FROM runs WHERE task='#5'").fetchone()
        self.assertEqual(row["five_hour_start"], 61)                          # recorded before any work, even on a room error

    def test_run_rows_record_the_runs_own_provider_weekly_window(self):
        root = Path(self.tmp.name)
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".claude" / "agents" / "builder.md").write_text("---\nmodel: sonnet\n---\nBuild.\n")
        now = time.time()
        self.ledger.record_usage([usage.observation("claude", "seven_day", 65, now + 86400, "test"),
                                  usage.observation("codex", "seven_day", 41, now + 86400, "test")], now=now)
        d = dispatch.Dispatcher(cfg_with(root), self.ledger, board=FakeBoard([]), log=lambda *_: None)
        d.prepare_worktree = lambda job, provider=None: (root / "wt", "b")
        d.remote_head = lambda branch: None
        d.preserve_work = d.cleanup_worktree = lambda *a, **k: None
        d.board.pr_for_branch = lambda branch: None
        def fake_run(provider, *a, **k):
            if provider == "codex":
                self.ledger.record_usage([usage.observation("codex", "seven_day", 50, now + 86400, "test")], now=now + 1)
            return workers.WorkerResult(ok=False, interrupted="test")
        orig = (dispatch.git, dispatch.run_worker)
        dispatch.git, dispatch.run_worker = (lambda args, cwd: ""), fake_run
        try:
            for n, provider in ((5, "codex"), (6, "claude")):
                try:
                    d.run_job(dispatch.Job("build", "builder", issue=issue(n, ["status:ready"]), reason="ready"), provider, "sonnet")
                except Exception:
                    pass
        finally:
            dispatch.git, dispatch.run_worker = orig
        rows = {r["task"]: (r["weekly_start"], r["weekly_end"]) for r in self.ledger.db.execute("SELECT task, weekly_start, weekly_end FROM runs")}
        self.assertEqual(rows.get("#5"), (41, 50))
        self.assertEqual(rows.get("#6"), (65, 65))

    def test_efficiency_reports_weekly_spend_per_provider(self):
        from commonroom import cli
        now = time.time()
        reset = now + 86400
        self.ledger.record_usage([usage.observation("claude", "seven_day", 60, reset, "t"),
                                  usage.observation("codex", "seven_day", 40, reset, "t")], now=now - 600)
        self.ledger.record_usage([usage.observation("claude", "seven_day", 65, reset, "t"),
                                  usage.observation("codex", "seven_day", 50, reset, "t")], now=now - 60)
        merge = self.ledger.start_run("t", "#1", "merge", "claude", now=now - 300)
        self.ledger.finish_run(merge, "done", now=now - 200)
        e = cli.efficiency(self.ledger, "t", now - 3600, now)
        self.assertEqual((e["claude_weekly_pct_spent"], e["codex_weekly_pct_spent"]), (5.0, 10.0))
        self.assertEqual(e["merges_per_weekly_pct"], 0.2)                      # from Claude's figure

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

    def update_board(self, heads_move):
        """Two approved, behind PRs (A=224 older, B=225). `update_branch` moves the head only if `heads_move`."""
        issues = [issue(200, ["status:review"]), issue(201, ["status:review"])]
        details = {224: self.approved_pr(224, "i200-a"), 225: self.approved_pr(225, "i201-b")}
        for n, det in details.items():
            det["headRefOid"] = f"{n}a"
        fake = FakeBoard(issues, prs=[{"number": 225, "headRefName": "i201-b"}, {"number": 224, "headRefName": "i200-a"}],
                         details=details)
        fake.behind = {224: 1, 225: 1}
        fake.update_calls = []

        def update(n):
            fake.update_calls.append(n)
            if heads_move:
                details[n]["headRefOid"] = f"{n}b"
                details[n]["commits"].append({"oid": f"{n}b", "committedDate": "2026-10-07T03:00:00Z"})
                fake.behind[n] = 0
        fake.update_branch = update
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        d.only_merges_main = lambda branch, oids: True                    # GitHub's update commit merges main and nothing else
        return fake, d

    def test_an_update_that_leaves_the_head_unchanged_is_not_repeated(self):
        """commonroom#133: gamemirage PR #653 was 'updated' ~50 times with no effect and held the one update slot."""
        fake, d = self.update_board(heads_move=False)
        d.housekeeping()                                                  # pass 1: A takes the slot
        self.assertEqual(fake.update_calls, [224])
        rows = [r for r in self.ledger.recent_runs("t") if r["role"] == "update"]
        self.assertEqual([(r["provider"], r["outputs"]) for r in rows],
                         [("none", [{"kind": "update", "ref": "PR #224", "head": "224a"}])])
        d.housekeeping()                                                  # pass 2: still behind at the same head
        self.assertEqual(fake.update_calls, [224, 225])                   # A not updated again; B gets the slot
        self.assertIn("merge-main run", dict(fake.comments)[200])         # A goes to a merge-main run
        self.assertIn((200, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)

    def test_a_normal_update_changes_the_head_and_is_not_handed_to_merge_main(self):
        fake, d = self.update_board(heads_move=True)
        d.housekeeping()
        self.assertEqual(fake.update_calls, [224])
        fake.behind[224] = 1                                              # main moved again, now behind the new head
        d.housekeeping()
        self.assertEqual(fake.update_calls, [224, 224])                   # a new head is a new update
        self.assertEqual(getattr(fake, "comments", []), [])
        self.assertEqual(getattr(fake, "label_calls", []), [])

    def test_behind_main_compares_from_the_branch_tip_when_pr_view_reports_an_older_head(self):
        """commonroom#133: `headRefOid` was c21b3ad (1 behind) while the branch tip 4ec9765 already contained main."""
        calls = []

        def gh(args):
            calls.append(args[1])
            return {"sha": "tip"} if "git/ref/heads/i1-x" in args[1] else {"behind_by": 0 if args[1].endswith("...tip") else 1}
        board = dispatch.GitHubBoard("o/r")
        detail = {"headRefOid": "old", "headRefName": "i1-x", "baseRefName": "main"}
        with mock.patch.object(dispatch.board_mod, "gh", gh):
            self.assertEqual(board.behind_main(detail), 0)
            self.assertEqual(len(calls), 3)                              # one extra call for the tip's ref
            calls.clear()
            self.assertEqual(board.behind_main(detail | {"headRefOid": "tip"}), 0)
            self.assertEqual(len(calls), 1)                              # not behind: no extra call

    def test_update_rows_are_not_model_time_for_the_share_gate(self):
        now = 1_000_000.0
        for _ in range(3):
            run = self.ledger.start_run("t", "PR #1", "update", "none", now=now - 600)
            self.ledger.finish_run(run, "done", now=now - 60)
        self.assertEqual(dispatch.run_minutes(self.ledger, "claude", now - 3600, now), {})
        self.assertEqual(dispatch.run_minutes(self.ledger, "codex", now - 3600, now), {})
        self.assertIsNone(dispatch.check_progress(self.ledger, "t", now))   # no model work, so no progress breaker

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

    def test_no_review_while_its_issue_is_building(self):
        """commonroom#124: #554's builder opened draft PR #627 to preserve work, and the next pick was an Opus review."""
        fake = FakeBoard([issue(200, ["status:in-progress"])], prs=[{"number": 224, "headRefName": "i200-x"}],
                         details={224: {"number": 224, "headRefName": "i200-x", "headRefOid": "224a",
                                        "commits": [{"oid": "224a", "committedDate": "2026-10-07T02:00:00Z"}],
                                        "comments": [], "statusCheckRollup": [{"conclusion": "SUCCESS"}]}})
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(True, "ok")}
        build = self.ledger.start_run("t", "#200", "builder", "claude")
        self.assertIsNone(d.pick_review(allowed))
        self.ledger.finish_run(build, "done")
        self.assertEqual(d.pick_review(allowed)[0].pr["number"], 224)

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

    def test_red_main_is_rerun_once_before_a_fix_task_opens(self):
        """commonroom#88: a flaky Godot crash on main cost a whole builder run; one rerun of the failed job passed."""
        run = "https://github.com/o/t/actions/runs/77"
        fake = FakeBoard([issue(200, ["status:review"])], prs=[{"number": 224, "headRefName": "i200-x"}],
                         details={224: self.approved_pr(224, "i200-x")})
        fake.merges = []
        fake.merge = fake.merges.append
        slow = {"name": "Slow simulation tests", "conclusion": "success", "sha": "a6b5a2c99", "runId": "77", "attempt": 1}

        def fast(conclusion, attempt):
            return [{"name": "Fast tests", "conclusion": conclusion, "sha": "a6b5a2c99", "detailsUrl": f"{run}/job/1",
                     "runUrl": run, "runId": "77", "attempt": attempt}, slow | {"attempt": attempt}]

        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        fake.main_runs = fast("failure", 1)                               # first attempt red: rerun, hold, no task
        actions = d.housekeeping()
        self.assertEqual(fake.reruns, ["77"])
        self.assertEqual(fake.merges, [])
        self.assertFalse(hasattr(fake, "created"))
        self.assertIn("main's CI is red (Fast tests): merges held, rerunning once before opening a fix task", actions)

        fake.main_runs = fast("in_progress", 2)                           # the rerun is running: still held
        self.assertTrue(any("being rerun" in a for a in d.housekeeping()))
        self.assertEqual((fake.reruns, fake.merges), (["77"], []))

        fake.main_runs = fast("failure", 2)                               # failed again: no second rerun, open the task
        d.housekeeping()
        self.assertEqual((fake.reruns, fake.merges), (["77"], []))
        self.assertEqual(fake.created[0][0], "Main is red: Fast tests failing on a6b5a2c")
        body = fake.created_bodies[0]
        self.assertIn("twice", body)
        self.assertIn(f"attempt 1 {run}/attempts/1, attempt 2 {run}/attempts/2", body)

        fake.main_runs = fast("success", 2)                               # a passing rerun: main green, merges go on
        del fake.created
        d.housekeeping()
        self.assertEqual(fake.merges, [224])
        self.assertFalse(hasattr(fake, "created"))
        self.assertEqual(fake.reruns, ["77"])

    def test_infra_only_red_main_is_not_rerun_or_held(self):
        fake = FakeBoard([issue(200, ["status:review"])], prs=[{"number": 224, "headRefName": "i200-x"}],
                         details={224: self.approved_pr(224, "i200-x")})
        fake.merges = []
        fake.merge = fake.merges.append
        fake.main_runs = [{"name": "Fast tests", "conclusion": "cancelled", "sha": "2", "runId": "78", "attempt": 1,
                           "detailsUrl": "https://github.com/o/t/actions/runs/78/job/1"}]
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        d.housekeeping()
        self.assertEqual(fake.merges, [224])
        self.assertFalse(hasattr(fake, "reruns") or hasattr(fake, "created"))

    def red_main_board(self, **fast):
        fake = FakeBoard([issue(200, ["status:review"])], prs=[{"number": 224, "headRefName": "i200-x"}],
                         details={224: self.approved_pr(224, "i200-x")})
        fake.merges = []
        fake.merge = fake.merges.append
        fake.main_runs = [{"name": "Fast tests", "conclusion": "failure", "sha": "a6b5a2c99", "runId": "77",
                           "runUrl": "https://github.com/o/t/actions/runs/77",
                           "detailsUrl": "https://github.com/o/t/actions/runs/77/job/1"} | fast]
        return fake

    def test_red_main_waits_for_a_running_run_before_rerunning(self):
        """commonroom#98: a failed hosted job beside queued self-hosted ones; every pass got "already running"."""
        fake = self.red_main_board(attempt=1, status="queued")
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = []
        self.assertTrue(d.main_red(fake.issues, actions))
        self.assertEqual(actions, ["merges held: main's CI is red (Fast tests); waiting for run 77 to finish before rerunning"])
        self.assertFalse(hasattr(fake, "reruns") or hasattr(fake, "created"))

        fake.main_runs[0]["status"] = "completed"                         # finished: now it is rerun once
        self.assertTrue(d.main_red(fake.issues, []))
        self.assertEqual(fake.reruns, ["77"])
        self.assertFalse(hasattr(fake, "created"))

    def test_a_rerun_that_errors_opens_the_fix_task(self):
        """commonroom#98: a rerun GitHub refuses kept attempt 1, so merges held every pass with no fix task."""
        fake = self.red_main_board(attempt=1, status="completed")

        def refuse(run_id):
            fake.reruns = getattr(fake, "reruns", []) + [run_id]
            raise RuntimeError("run 77 cannot be rerun; it is too old")

        fake.rerun_failed = refuse
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        self.assertTrue(d.main_red(fake.issues, []))
        self.assertEqual([t for t, *_ in fake.created], ["Main is red: Fast tests failing on a6b5a2c"])
        self.assertIn("rerun of it failed: run 77 cannot be rerun; it is too old", fake.created_bodies[0])
        self.assertNotIn("twice", fake.created_bodies[0])

        fake.issues.append(issue(900, ["status:ready"]) | {"title": "Main is red: Fast tests failing on a6b5a2c"})
        actions = []
        self.assertTrue(d.main_red(fake.issues, actions))                 # the open task is reported, not doubled
        self.assertEqual(len(fake.created), 1)
        self.assertIn("merges held: main's CI is red (Fast tests); #900 fixes it", actions)

    def test_unknown_attempt_opens_the_fix_task_without_a_rerun(self):
        """commonroom#98: the run list failed, so the attempt is unknown; hold merges and open the task, as before #88."""
        fake = self.red_main_board()
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        d.housekeeping()
        self.assertEqual(fake.merges, [])
        self.assertFalse(hasattr(fake, "reruns"))
        self.assertEqual(len(fake.created), 1)

    def counting_main_checks(self, fake):
        fake.main_reads = 0

        def read():
            fake.main_reads += 1
            return fake.main_runs

        fake.main_checks = read

    def test_red_main_is_rerun_on_a_pass_with_no_mergeable_pr(self):
        """commonroom#111: gamemirage's main sat red 1 h 42 min until a PR was approved and the merge loop looked."""
        fake = self.red_main_board(attempt=1, status="completed")
        fake.prs, fake.details = [], {}
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertEqual(fake.reruns, ["77"])
        self.assertFalse(hasattr(fake, "created"))
        self.assertIn("main's CI is red (Fast tests): merges held, rerunning once before opening a fix task", actions)

    def test_red_main_after_its_rerun_opens_the_fix_task_with_no_mergeable_pr(self):
        """commonroom#111: the second-attempt failure opens the fix task without waiting for an approval."""
        fake = self.red_main_board(attempt=2, status="completed")
        fake.details[224]["comments"] = []                                # PR open but not approved: not mergeable
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        d.housekeeping()
        self.assertFalse(hasattr(fake, "reruns"))
        self.assertEqual([t for t, *_ in fake.created], ["Main is red: Fast tests failing on a6b5a2c"])
        self.assertEqual(fake.merges, [])

    def test_main_checks_are_read_once_per_pass(self):
        """commonroom#111: the merge loop reuses the pass's red-main result instead of reading main again."""
        fake = FakeBoard([issue(200, ["status:review"]), issue(201, ["status:review"])],
                         prs=[{"number": 224, "headRefName": "i200-x"}, {"number": 225, "headRefName": "i201-y"}],
                         details={224: self.approved_pr(224, "i200-x"), 225: self.approved_pr(225, "i201-y")})
        fake.merges = []
        fake.merge = fake.merges.append
        fake.main_runs = [{"name": "Fast tests", "conclusion": "success", "sha": "1"}]
        self.counting_main_checks(fake)
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        d.housekeeping()
        self.assertEqual(fake.main_reads, 1)
        self.assertEqual(fake.merges, [224])

        fake.main_runs = [{"name": "Fast tests", "conclusion": "failure", "sha": "2", "runId": "77", "attempt": 1,
                           "status": "completed", "detailsUrl": "https://github.com/o/t/actions/runs/77/job/1"}]
        fake.main_reads = 0
        d.housekeeping()                                                  # red: held, still one read
        self.assertEqual(fake.main_reads, 1)
        self.assertEqual(fake.merges, [224])
        self.assertEqual(fake.reruns, ["77"])

    def test_green_main_with_no_prs_changes_nothing(self):
        fake = FakeBoard([issue(200, ["status:ready"])])
        fake.main_runs = [{"name": "Fast tests", "conclusion": "success", "sha": "1", "runId": "77", "attempt": 1,
                           "status": "completed", "detailsUrl": "https://github.com/o/t/actions/runs/77/job/1"}]
        self.counting_main_checks(fake)
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertEqual(fake.main_reads, 1)
        self.assertFalse(hasattr(fake, "reruns") or hasattr(fake, "created"))
        self.assertFalse(any("main" in a for a in actions))

    def test_without_ci_main_is_not_read_on_a_pass_with_no_mergeable_pr(self):
        """commonroom (`ci = false`) has no checks on main: an idle pass doesn't ask GitHub for them."""
        cfg = cfg_with(self.tmp.name)
        cfg["checks"] = {"ci": False}
        fake = FakeBoard([issue(200, ["status:ready"])])
        self.counting_main_checks(fake)
        dispatch.Dispatcher(cfg, self.ledger, board=fake, log=lambda *_: None).housekeeping()
        self.assertEqual(fake.main_reads, 0)

    def stale_runs_board(self):
        run = lambda i, branch, sha, event="pull_request": {"databaseId": i, "headBranch": branch, "headSha": sha, "event": event}
        fake = FakeBoard([issue(200, ["status:ready"])],
                         prs=[{"number": 7, "headRefName": "i7-x", "headRefOid": "new1111"}])
        fake.queued = [run(1, "i5-merged", "aaa1111"), run(2, "i7-x", "new1111"), run(3, "i7-x", "old2222"),
                       run(4, "main", "bbb3333", "push"), run(5, "i9-gone", "ccc4444", "schedule")]
        fake.details = {7: {"number": 7, "headRefName": "i7-x", "mergeable": "MERGEABLE", "commits": [],
                            "comments": [], "statusCheckRollup": []}}
        return fake

    def test_housekeeping_cancels_queued_runs_of_merged_branches_and_superseded_heads(self):
        """commonroom#138: exactly the merged-branch run and the older-sha run; current head and non-PR runs stay."""
        fake = self.stale_runs_board()
        actions = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None).housekeeping()
        self.assertEqual(fake.cancelled, [1, 3])
        self.assertTrue(any("run 1 on i5-merged" in a and "merged or closed" in a for a in actions))
        self.assertTrue(any("run 3 on i7-x" in a for a in actions))

    def test_in_progress_runs_are_never_cancelled(self):
        runs = [{"databaseId": 8, "headBranch": "i5-merged", "headSha": "a", "event": "pull_request", "status": "in_progress"}]
        self.assertEqual(dispatch.stale_ci_runs(runs, []), [])

    def test_without_ci_no_run_is_listed_or_cancelled(self):
        cfg = cfg_with(self.tmp.name)
        cfg["checks"] = {"ci": False}
        fake = self.stale_runs_board()
        dispatch.Dispatcher(cfg, self.ledger, board=fake, log=lambda *_: None).housekeeping()
        self.assertFalse(hasattr(fake, "listed"))
        self.assertEqual(fake.cancelled, [])

    def test_a_failing_run_list_cancels_nothing_and_the_pass_goes_on(self):
        fake = self.stale_runs_board()
        fake.queued_error = "gh down"
        actions = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None).housekeeping()
        self.assertEqual(fake.cancelled, [])
        self.assertTrue(any("couldn't list queued CI runs" in a for a in actions))

    def test_a_cancel_that_errors_does_not_stop_the_rest(self):
        fake = self.stale_runs_board()
        def cancel(run_id):
            fake.cancelled = fake.cancelled + [run_id]
            if run_id == 1:
                raise RuntimeError("nope")
        fake.cancel_run = cancel
        actions = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None).housekeeping()
        self.assertEqual(fake.cancelled, [1, 3])
        self.assertTrue(any("couldn't cancel" in a for a in actions))

    def test_dry_run_neither_reads_nor_reruns_a_red_main(self):
        fake = self.red_main_board(attempt=1, status="completed")
        fake.prs, fake.details = [], {}
        self.counting_main_checks(fake)
        dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None, dry_run=True).housekeeping()
        self.assertEqual(fake.main_reads, 0)
        self.assertFalse(hasattr(fake, "reruns") or hasattr(fake, "created"))

    def red_pr(self, n, branch, ended="2026-10-07T03:00:00Z"):
        return {"number": n, "headRefName": branch, "mergeable": "MERGEABLE", "commits": [], "comments": [],
                "statusCheckRollup": [{"name": "Fast tests", "conclusion": "FAILURE", "completedAt": ended,
                                       "detailsUrl": f"https://github.com/o/t/actions/runs/{n}/job/1"}]}

    def test_red_main_leaves_other_red_prs_alone_until_it_is_fixed(self):
        """commonroom#43: while main is red, a PR fails for main's reason; bouncing it costs a builder run for nothing."""
        fix = issue(900, ["status:review"]) | {"title": "Main is red: Fast tests failing on 1f411c0"}
        issues = [issue(200, ["status:review"]), fix]
        fake = FakeBoard(issues, prs=[{"number": 224, "headRefName": "i200-x"}, {"number": 901, "headRefName": "i900-fix-main"}],
                         details={224: self.red_pr(224, "i200-x"), 901: self.red_pr(901, "i900-fix-main")})
        fake.comments = []
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertFalse(any(n == 200 for n, *_ in fake.label_calls))      # not relabelled...
        self.assertFalse(any(n == 200 for n, _ in fake.comments))          # ...nor commented on
        self.assertIn("PR #224: CI red while main is red; left alone until #900 lands", actions)
        self.assertIn((900, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)
        self.assertTrue(any(n == 900 and "CI is red on PR #901" in body for n, body in fake.comments))

        # Main's fix lands: the PR that failed before it gets main merged in so CI reruns, not a builder run.
        fix |= {"state": "CLOSED", "closedAt": "2026-10-07T04:00:00Z"}
        issues[1] = fix
        fake.prs.pop()
        fake.behind = {224: 1}
        fake.label_calls, fake.comments = [], []
        actions = d.housekeeping()
        self.assertEqual(fake.updated, [224])
        self.assertEqual((fake.label_calls, fake.comments), ([], []))

        # CI on the updated branch fails too: that one is the PR's own, and goes back to its builder.
        fake.details[224] = self.red_pr(224, "i200-x", ended="2026-10-07T05:00:00Z")
        d.housekeeping()
        self.assertEqual(fake.updated, [224])
        self.assertIn((200, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)

    def test_a_main_is_red_issue_changes_nothing_without_ci(self):
        """commonroom#43: commonroom (`ci = false`) merges on its reviewer's tested approval, Main is red issue or not."""
        cfg = cfg_with(self.tmp.name)
        cfg["checks"] = {"ci": False}
        fix = issue(900, ["status:ready"]) | {"title": "Main is red: Fast tests failing on 1f411c0"}
        pr = self.approved_pr(224, "i200-x") | {"headRefOid": "224aaaa", "statusCheckRollup": []}
        pr["comments"][0]["body"] += "\nTESTS: PASS 224aaaa: Ran 9 tests OK"
        fake = FakeBoard([issue(200, ["status:review"]), fix], prs=[{"number": 224, "headRefName": "i200-x"}], details={224: pr})
        fake.merges = []
        fake.merge = fake.merges.append
        d = dispatch.Dispatcher(cfg, self.ledger, board=fake, log=lambda *_: None)
        d.housekeeping()
        self.assertEqual(fake.merges, [224])
        self.assertFalse(any(n == 200 and "needs:changes" in add for n, add, _ in fake.label_calls))

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


    def test_approved_pr_already_on_main_is_closed_not_bounced(self):
        """commonroom#42: gamemirage PR #238 was all on main once #255 (built on it) merged, yet went back to its
        builder for a rebase that would have left it empty."""
        def fake_git(own_diff):
            calls = []
            def run(args, cwd):
                calls.append(args)
                return own_diff if args[0] == "diff" else ""
            return calls, run

        def setup(mergeable="CONFLICTING", review=True):
            detail = self.approved_pr(238, "i230-x") | {"mergeable": mergeable, "files": [{"path": "a.gd"}, {"path": "b.gd"}]}
            if not review:
                detail["comments"] = []
            fake = FakeBoard([issue(230, ["status:review"])], prs=[{"number": 238, "headRefName": "i230-x"}],
                             details={238: detail})
            fake.merges = []
            fake.merge = fake.merges.append
            return fake, dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)

        original = dispatch.git
        self.addCleanup(setattr, dispatch, "git", original)

        calls, dispatch.git = fake_git("")                                # empty own-diff, conflicting: close it
        fake, d = setup()
        actions = d.housekeeping()
        self.assertIn(["diff", "--name-only", "origin/main", "origin/i230-x", "--", "a.gd", "b.gd"], calls)
        self.assertEqual([n for n, _ in fake.closed_prs], [238])
        self.assertEqual([n for n, _ in fake.closed], [230])
        self.assertIn("Superseded", fake.closed_prs[0][1])
        self.assertEqual(fake.label_calls, [])                            # not bounced to its builder
        self.assertFalse(getattr(fake, "comments", []))
        self.assertTrue(any("closed as superseded" in a for a in actions))

        calls, dispatch.git = fake_git("")                                # empty own-diff, behind main: close, no update
        fake, d = setup(mergeable="MERGEABLE")
        fake.behind = {238: 2}
        d.housekeeping()
        self.assertEqual([n for n, _ in fake.closed_prs], [238])
        self.assertFalse(getattr(fake, "updated", []))
        self.assertEqual(fake.merges, [])

        calls, dispatch.git = fake_git("a.gd")                            # own-diff not empty: left alone, bounced as before
        fake, d = setup()
        d.housekeeping()
        self.assertFalse(getattr(fake, "closed_prs", []))
        self.assertFalse(getattr(fake, "closed", []))
        self.assertIn((230, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)
        self.assertIn("merge-main run", fake.comments[0][1])

        calls, dispatch.git = fake_git("a.gd")                            # not empty and behind: updated as before
        fake, d = setup(mergeable="MERGEABLE")
        fake.behind = {238: 2}
        d.housekeeping()
        self.assertFalse(getattr(fake, "closed_prs", []))
        self.assertEqual(fake.updated, [238])

        calls, dispatch.git = fake_git("")                                # unapproved: never checked, never closed
        fake, d = setup(review=False)
        d.housekeeping()
        self.assertEqual(calls, [])
        self.assertFalse(getattr(fake, "closed_prs", []))
        self.assertIn((230, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), fake.label_calls)

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

    def test_continued_build_ends_in_review_with_no_other_status(self):
        """commonroom#93: gamemirage#427 ended with status:ready, status:review and needs:changes after a builder
        continued from its preserved branch. The PR's CHANGES verdict stays current until the builder pushes, so
        housekeeping bounced the issue back to ready mid-build."""
        work = issue(427, ["status:review", "role:builder"])
        detail = {"commits": [{"committedDate": "2026-10-11T12:00:00Z"}],
                  "comments": [{"createdAt": "2026-10-11T13:09:00Z", "body": "[agent:reviewer] REVIEW: CHANGES - fix it"}],
                  "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        fake = LabelBoard([work], prs=[{"number": 499, "headRefName": "i427-x"}], details={499: detail})
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        state = lambda: {l for l in dispatch.labels(work) if l.startswith(("status:", "needs:"))}

        d.housekeeping()                                                     # reviewer CHANGES: back to its builder
        self.assertEqual(state(), {"status:ready", "needs:changes"})
        fake.set_labels(427, add=["status:in-progress"], remove=["status:ready", "needs:changes"])   # the claim in run()
        self.ledger.start_run("t", "#427", "builder", "codex")
        fake.label_calls.clear()
        d.housekeeping()                                                     # mid-build, the CHANGES is still current
        self.assertEqual(fake.label_calls, [])
        self.assertEqual(state(), {"status:in-progress"})

        fake.pr_for_branch = lambda branch: {"number": 499, "state": "OPEN"}
        d.remote_head = lambda branch: "new"
        job = dispatch.Job("build", "builder", issue=work, reason="ready")
        status, _, _ = d.assess(job, "i427-x", "old", "", workers.WorkerResult(ok=True, final_text="Fixed.\nSTATUS: DONE"))
        self.assertEqual(status, "done")
        self.assertEqual(state(), {"status:review"})

        # Even if a stray ready and needs:changes landed meanwhile, the move to review drops them in one call.
        work["labels"] = [{"name": l} for l in ("status:ready", "status:in-progress", "needs:changes", "role:builder")]
        fake.label_calls.clear()
        d.assess(job, "i427-x", "old", "", workers.WorkerResult(ok=True, final_text="Fixed.\nSTATUS: DONE"))
        self.assertEqual(len(fake.label_calls), 1)
        self.assertEqual(state(), {"status:review"})
        d.remote_head = lambda branch: "old"                                 # nothing pushed, STATUS: DONE: same move
        work["labels"] = [{"name": l} for l in ("status:ready", "status:in-progress", "needs:changes")]
        d.assess(job, "i427-x", "old", "", workers.WorkerResult(ok=True, final_text="Nothing left.\nSTATUS: DONE"))
        self.assertEqual(state(), {"status:review"})

    def test_housekeeping_clears_ready_from_issues_in_review_or_in_progress(self):
        """commonroom#93: a ready label next to review or in-progress invites a second builder."""
        both = [issue(1, ["status:ready", "status:review"]), issue(2, ["status:ready", "status:in-progress"]),
                issue(3, ["status:ready"]), issue(4, ["status:ready", "status:review"], state="CLOSED")]
        fake = LabelBoard(both, prs=[{"number": 9, "headRefName": "i1-x"}],
                          details={9: {"commits": [{"committedDate": "2026-10-11T12:00:00Z"}], "comments": []}})
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        actions = d.housekeeping()
        self.assertEqual(fake.label_calls, [(1, (), ("status:ready",)), (2, (), ("status:ready",))])
        self.assertEqual(dispatch.labels(both[0]), {"status:review"})
        self.assertEqual(dispatch.labels(both[1]), {"status:in-progress"})
        self.assertIn("#1 had status:ready while review: removed status:ready", actions)
        self.assertIn("#2 had status:ready while in-progress: removed status:ready", actions)

    def test_a_quoted_verdict_in_a_builder_reply_is_not_a_review(self):
        """commonroom#58: on PR #54 the builder's reply quoted `REVIEW: CHANGES` and housekeeping escalated #49."""
        work = issue(80, ["status:review"])
        detail = {"commits": [{"committedDate": "2026-10-09T09:00:00Z"}],
                  "comments": [{"createdAt": "2026-10-09T09:30:00Z", "body": "[agent:reviewer] REVIEW: CHANGES - fix x"}],
                  "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        fake = FakeBoard([work], prs=[{"number": 81, "headRefName": "i80-x"}], details={81: detail})
        fake.merges = []
        fake.merge = fake.merges.append
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=fake, log=lambda *_: None)
        d.housekeeping()                                                     # round 1: back to the builder
        fake.label_calls.clear()
        detail["commits"].append({"committedDate": "2026-10-09T10:00:00Z"})  # the fix, and a reply quoting a verdict
        detail["comments"].append({"createdAt": "2026-10-09T10:05:00Z", "body": "[agent:builder] Fixed: a director "
                                   "`REVIEW: CHANGES` after the last commit now sends the PR back."})
        self.assertEqual(dispatch.changes_rounds(detail), 1)
        self.assertIsNone(dispatch.latest_review(detail))
        d.housekeeping()
        self.assertFalse(any("needs:director" in call[1] for call in fake.label_calls))   # not escalated
        self.assertTrue(d.needs_review(detail))                              # the reviewer looks at the fix

        # With CI green (gamemirage), a builder quoting an approval after the last commit doesn't merge it either.
        detail["comments"].append({"createdAt": "2026-10-09T10:10:00Z", "body": "[agent:builder] Waiting for "
                                   "REVIEW: APPROVE from the reviewer."})
        self.assertIsNone(dispatch.latest_review(detail))
        d.housekeeping()
        self.assertEqual(fake.merges, [])
        self.assertTrue(d.needs_review(detail))
        detail["comments"].append({"createdAt": "2026-10-09T10:30:00Z", "body": "REVIEW: APPROVE\nLGTM"})   # no prefix
        self.assertEqual(dispatch.latest_review(detail), "APPROVE")
        d.housekeeping()
        self.assertEqual(fake.merges, [81])

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


class HousekeepingFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, board, dry_run=False):
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None, dry_run=dry_run)
        d.refresh_readings = lambda now: None
        d.allowed = lambda now: {"claude": gate.Verdict(True, "ok")}
        d.run_job = lambda job, provider, model: {}
        return d

    def broken_board(self):
        board = FakeBoard([issue(4, ["status:ready", "role:builder"])])
        def boom():
            raise KeyError("labels")
        board.all_issues = boom
        return board

    def test_a_failing_housekeeping_still_picks_a_job(self):
        board = self.broken_board()
        report = self.dispatcher(board).run_once()
        self.assertEqual(report["housekeeping"], ["housekeeping failed: KeyError: 'labels'"])
        self.assertEqual(report["job"]["kind"], "build")

    def test_one_issue_per_exception_type_and_none_in_dry_run(self):
        board = self.broken_board()
        d = self.dispatcher(board)
        d.run_once()
        self.assertEqual([c[:2] for c in board.created], [("[room] Housekeeping is failing: KeyError", ("needs:director",))])
        board.issues.append(issue(900, [], milestone=None) | {"title": "[room] Housekeeping is failing: KeyError"})
        d.run_once()
        self.assertEqual(len(board.created), 1)
        dry = self.broken_board()
        self.dispatcher(dry, dry_run=True).run_once()
        self.assertFalse(hasattr(dry, "created"))

    def test_a_working_housekeeping_report_is_unchanged(self):
        board = FakeBoard([issue(4, ["status:ready", "role:builder"])])
        report = self.dispatcher(board).run_once()
        self.assertEqual(report["housekeeping"], [])
        self.assertFalse(hasattr(board, "created"))

    def test_system_exit_still_propagates(self):
        board = self.broken_board()
        board.all_issues = lambda: (_ for _ in ()).throw(SystemExit(1))
        with self.assertRaises(SystemExit):
            self.dispatcher(board).run_once()


class StaleClaims(unittest.TestCase):
    """commonroom#63: a moonshot claimed gamemirage#296, the pace gate stopped it a minute later, and the issue sat
    `status:in-progress` with no run on it for 1.5 h. Housekeeping releases a claim no run holds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    @staticmethod
    def claimed(minutes_ago, n=296):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))
        return issue(n, ["status:in-progress", "prio:top"]) | {"updatedAt": stamp}

    def housekeep(self, issues, prs=()):
        board = FakeBoard(issues, prs=list(prs))
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None)
        return board, d.housekeeping()

    def test_releases_an_unheld_claim_after_30_minutes(self):
        board, actions = self.housekeep([self.claimed(31)])
        self.assertEqual(board.label_calls, [(296, ("status:ready",), ("status:in-progress",))])
        self.assertEqual(board.comments, [(296, "Released a stale claim: no room or moonshot run is working on this and "
                                                "it hasn't changed for 31 min. Back to ready.")])
        self.assertIn("released stale claim #296", actions)

    def test_leaves_a_recent_claim_alone(self):
        board, actions = self.housekeep([self.claimed(29)])
        self.assertEqual((board.label_calls, actions), ([], []))

    def test_leaves_a_claim_held_by_a_running_run_alone(self):
        self.ledger.start_run("t", "#296", "builder", "claude")
        board, actions = self.housekeep([self.claimed(31)])
        self.assertEqual((board.label_calls, actions), ([], []))

    def test_a_running_moonshot_holds_every_claim(self):
        self.ledger.start_run("t", "moonshot:harvest_sprint", "moonshot", "claude", "fable")
        board, actions = self.housekeep([self.claimed(31)])
        self.assertEqual((board.label_calls, actions), ([], []))

    def test_other_projects_runs_hold_nothing(self):
        self.ledger.start_run("other", "#296", "builder", "claude")
        self.ledger.start_run("other", "moonshot:x", "moonshot", "claude")
        board, actions = self.housekeep([self.claimed(31)])
        self.assertIn("released stale claim #296", actions)

    def test_an_issue_with_an_open_pr_is_in_review(self):
        prs = [{"number": 400, "headRefName": "i296-harvest", "labels": []}]
        detail = {"number": 400, "headRefName": "i296-harvest", "commits": [], "comments": [], "statusCheckRollup": []}
        board = FakeBoard([self.claimed(31)], prs=prs, details={400: detail})
        dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=lambda *_: None).housekeeping()
        self.assertEqual(board.label_calls, [])

    def test_digest_lists_stale_claims(self):
        from commonroom import cli

        class FakeGh:   # stands in for the `board` module the digest reads GitHub through
            issues = staticmethod(lambda repo, **_: [self.claimed(31), self.claimed(5, n=297)])
            milestone = staticmethod(lambda repo, title: None)
            gh = staticmethod(lambda args: [])

        cfg = cfg_with(self.tmp.name)
        orig = cli.Ledger, cli.board
        cli.Ledger, cli.board = (lambda: Ledger(self.ledger.path)), FakeGh
        try:
            result = cli.digest(cfg, 1)
        finally:
            cli.Ledger, cli.board = orig
        self.assertEqual([c["number"] for c in result["stale_claims"]], [296])


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
    """Real git: a main checkout with the room's worktrees in its default folder next to it, as on the owner's PC."""

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
        self.base = self.d.worktree_base()
        self.legacy = self.root / ".claude" / "worktrees"   # the default before commonroom#48, Claude Code's own folder

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

    def test_the_default_base_is_outside_the_project_and_outside_claude_codes_folder(self):
        self.assertEqual(self.base, self.root.parent / "game-wt" / "room")
        self.git("init", "-q", "--bare", str(Path(self.tmp.name) / "origin.git"))
        self.git("remote", "add", "origin", str(Path(self.tmp.name) / "origin.git"))
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin")
        leftover = self.legacy / "i5-issue-5"                                       # left by a run before the switch
        self.git("worktree", "add", "-q", "-b", "i5-issue-5", str(leftover), "main")
        claude = self.legacy / "agent-a28b11fedd424a3e8"                            # Claude Code's own subagent worktree
        self.git("worktree", "add", "-q", "--detach", str(claude), "main")
        path, branch = self.d.prepare_worktree(dispatch.Job("build", "builder", issue=self.board.issues[0]), "claude")
        self.assertEqual((path, branch), (self.base / "i5-issue-5", "i5-issue-5"))
        self.assertTrue((path / ".git").exists())
        self.assertFalse(leftover.exists())                                         # it held the branch: removed
        self.assertTrue((claude / ".git").exists())                                 # never touched
        path, _ = self.d.prepare_worktree(dispatch.Job("audit", "auditor"), "claude")
        self.assertEqual(path.parent, self.base)

    def test_orphan_recovery_finds_room_worktrees_in_the_legacy_folder_and_nothing_else(self):
        wt = self.legacy / "i5-x"
        self.git("worktree", "add", "-q", "-b", "i5-x", str(wt), "main")
        (wt / "work.txt").write_text("unsaved")
        others = [self.legacy / name for name in ("agent-a1", "ms-i5-x", "day-1", "i6-y")]
        for other in others:
            other.mkdir(parents=True); (other / "keep.txt").write_text("k")
        self.ledger.start_run("t", "#5", "builder", "claude", now=time.time() - 3 * 3600)   # its dispatcher died
        self.assertEqual(len(self.d.recover_orphans()), 1)
        self.assertFalse(wt.exists())                                               # recovered and removed...
        self.assertTrue(any(line.strip().startswith(f"removing worktree {wt}") for line in self.lines))
        self.assertIn("unfinished work preserved", self.git("log", "-1", "--format=%B", "i5-x"))       # ...after its work was saved
        for other in others:
            self.assertTrue((other / "keep.txt").exists(), other)                   # not the room's, or not this task's

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

    def test_main_checks_carry_each_runs_id_and_attempt(self):
        """commonroom#88: the attempt comes from GitHub, so a restarted dispatcher can't rerun the same run again."""
        def fake_gh(args, timeout=30):
            if args[0] == "api":
                return [{"name": "Fast tests", "conclusion": "failure", "sha": "a",
                         "detailsUrl": "https://github.com/o/t/actions/runs/77/job/1"},
                        {"name": "Other", "conclusion": "success", "sha": "a", "detailsUrl": "https://ci.example/9"}]
            if args[:2] == ["run", "list"]:
                return [{"databaseId": 77, "attempt": 2}]
            return None

        orig = dispatch.board_mod.gh
        dispatch.board_mod.gh = fake_gh
        try:
            fast, other = dispatch.GitHubBoard("o/t").main_checks()
        finally:
            dispatch.board_mod.gh = orig
        self.assertEqual((fast["runId"], fast["attempt"], fast["runUrl"]), ("77", 2, "https://github.com/o/t/actions/runs/77"))
        self.assertNotIn("runId", other)

    def test_main_checks_survive_a_failed_run_list(self):
        """commonroom#98: a `gh run list` error made main_red() merge as usual on a red main."""
        def fake_gh(args, timeout=30):
            if args[0] == "api":
                return [{"name": "Fast tests", "conclusion": "failure", "sha": "a",
                         "detailsUrl": "https://github.com/o/t/actions/runs/77/job/1"}]
            if args[:2] == ["run", "list"]:
                raise RuntimeError("HTTP 502")
            return None

        orig = dispatch.board_mod.gh
        dispatch.board_mod.gh = fake_gh
        tmp = tempfile.TemporaryDirectory()
        ledger = Ledger(Path(tmp.name) / "room.sqlite3")
        try:
            board = dispatch.GitHubBoard("o/t")
            (fast,) = board.main_checks()
            fake = FakeBoard([])
            fake.main_checks = board.main_checks
            d = dispatch.Dispatcher(cfg_with(tmp.name), ledger, board=fake, log=lambda *_: None)
            held = d.main_red([], [])
        finally:
            dispatch.board_mod.gh = orig
            ledger.close(); tmp.cleanup()
        self.assertEqual(fast["runId"], "77")
        self.assertNotIn("attempt", fast)
        self.assertNotIn("status", fast)
        self.assertTrue(held)
        self.assertEqual([t for t, *_ in fake.created], ["Main is red: Fast tests failing on a"])

    def test_main_checks_carry_each_runs_status(self):
        def fake_gh(args, timeout=30):
            if args[0] == "api":
                return [{"name": "Fast tests", "conclusion": "failure", "sha": "a",
                         "detailsUrl": "https://github.com/o/t/actions/runs/77/job/1"}]
            if args[:2] == ["run", "list"]:
                self.assertEqual(args[-1], "databaseId,attempt,status")
                return [{"databaseId": 77, "attempt": 1, "status": "in_progress"}]
            return None

        orig = dispatch.board_mod.gh
        dispatch.board_mod.gh = fake_gh
        try:
            (fast,) = dispatch.GitHubBoard("o/t").main_checks()
        finally:
            dispatch.board_mod.gh = orig
        self.assertEqual((fast["attempt"], fast["status"]), (1, "in_progress"))

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


class BouncedPrOrder(unittest.TestCase):
    @staticmethod
    def order(*specs):
        issues = [{"number": n, "labels": [{"name": l} for l in names]} for n, *names in specs]
        return [i["number"] for i in sorted(issues, key=dispatch.priority)]

    def test_needs_changes_beats_high_older_and_newer(self):
        self.assertEqual(self.order((5, "prio:high"), (7, "needs:changes"), (9, "prio:high")), [7, 5, 9])
        self.assertEqual(self.order((9, "prio:high"), (7, "needs:changes"), (5, "prio:high")), [7, 5, 9])

    def test_low_needs_changes_beats_unlabelled_and_high(self):
        self.assertEqual(self.order((1, "prio:high"), (2,), (3, "prio:low", "needs:changes")), [3, 1, 2])

    def test_top_beats_needs_changes(self):
        self.assertEqual(self.order((8, "needs:changes", "prio:high"), (9, "prio:top")), [9, 8])

    def test_between_bounced_tier_then_oldest(self):
        self.assertEqual(self.order((4, "needs:changes"), (3, "needs:changes", "prio:low"),
                                    (6, "needs:changes", "prio:high"), (5, "needs:changes", "prio:high")),
                         [5, 6, 4, 3])

    def test_without_needs_changes_order_unchanged(self):
        self.assertEqual(self.order((4, "prio:low"), (3,), (9, "prio:high"), (1, "prio:top")), [1, 9, 3, 4])
