"""commonroom#31: the room serves itself. Guest share of the plan, the no-CI merge rule, the live checkout update,
and the host project (gamemirage) behaving exactly as before."""
import ast
import os
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from commonroom import config, dispatch, gate, usage
from commonroom.ledger import Ledger

REPO = Path(__file__).resolve().parent.parent
GAMEMIRAGE = Path("D:/Dev/gamemirage/room.toml")
HEAD = "0123456789abcdef0123456789abcdef01234567"
TEST = "python -m unittest discover -s tests"


class MergeBoard:
    def __init__(self, prs, details, issues=()):
        self.prs, self.details, self.issues, self.merged, self.labels = prs, details, list(issues), [], []
        self.behind, self.updated, self.cancelled = {}, [], []

    def behind_main(self, detail): return self.behind.get(detail["headRefOid"], 0)
    def update_branch(self, n): self.updated.append(n)
    def main_checks(self): return []
    def queued_runs(self): return []
    def cancel_run(self, run_id): self.cancelled.append(run_id)
    def all_issues(self): return self.issues
    def open_issues(self, milestone): return [i for i in self.issues if i["state"] == "OPEN"]
    def open_prs(self): return self.prs
    def pr_detail(self, n): return self.details[n]
    def merge(self, n): self.merged.append(n)
    def set_labels(self, n, add=(), remove=()): self.labels.append((n, tuple(add), tuple(remove)))
    def comment(self, n, body): pass


def pr(n, issue_no, labels=()):
    return {"number": n, "headRefName": f"i{issue_no}-thing", "labels": [{"name": l} for l in labels]}


def detail(body, head=HEAD, checks=(), committed="2026-10-09T12:00:00Z", reviewed="2026-10-09T13:00:00Z"):
    return {"headRefOid": head, "commits": [{"oid": head, "committedDate": committed}],
            "comments": [{"createdAt": reviewed, "body": body}] if body else [], "statusCheckRollup": list(checks)}


def approve(sha=HEAD[:7], verdict="PASS", command=TEST):
    return f"[agent:reviewer] REVIEW: APPROVE\nTESTS: {verdict} {sha} {command}: Ran 80 tests in 3.0s OK\nLooks right."


def project_cfg(root, name="commonroom", ci=False, share=None, host=None):
    cfg = config.merge(config.DEFAULTS, {"project": {"name": name, "repo": f"o/{name}"}, "goal": {"milestone": "M"},
                                         "checks": {"test": TEST} | ({} if ci else {"ci": False}),
                                         "share": share or {}, "roles": {"reviewer": ["claude:opus"]}})
    cfg["_root"], cfg["min_free_gb"] = str(root), {"room": 0, "project": 0}
    if host:
        cfg["_host"] = host
    return cfg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")
        self._owner = usage.owner_active
        usage.owner_active = lambda *a, **k: None   # this machine's real Claude sessions mustn't sway the tests

    def tearDown(self):
        usage.owner_active = self._owner
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, cfg, board):
        return dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None)

    def run_for(self, project, provider, minutes, ago_minutes, now):
        run = self.ledger.start_run(project, None, "builder", provider, now=now - ago_minutes * 60)
        self.ledger.finish_run(run, "done", outputs=[{"kind": "pr", "ref": "x"}], now=now - (ago_minutes - minutes) * 60)

    def fresh_readings(self, now, weekly=5):
        for provider in ("claude", "codex"):
            self.ledger.record_usage([usage.observation(provider, "five_hour", 5, now + 3600, "test"),
                                      usage.observation(provider, "seven_day", weekly, now + 3 * 86400, "test")], now=now)


class Config(unittest.TestCase):
    def test_this_repo_is_a_guest_project_without_ci(self):
        cfg = config.load(REPO / "room.toml", follow_host=False)
        self.assertEqual((cfg["project"]["name"], cfg["project"]["repo"]), ("commonroom", "kgaydhani/commonroom"))
        self.assertEqual(cfg["goal"]["milestone"], "Room v3: the room improves itself")
        self.assertFalse(config.has_ci(cfg))
        self.assertEqual(cfg["checks"]["test"], TEST)
        self.assertLess(cfg["share"]["max"], 0.5)                       # a modest share, not half the plan
        self.assertEqual(cfg["parallel"]["claude_slots"], 1)
        self.assertTrue(cfg["project"]["worktrees"].startswith("D:/Dev/commonroom-wt/"))
        for role in ("builder", "reviewer", "director", "scout"):          # every role it can dispatch has a prompt
            self.assertTrue((REPO / ".claude" / "agents" / f"{role}.md").is_file(), role)
        self.assertTrue((REPO / "AGENTS.md").is_file())

    def test_guest_takes_the_hosts_budget_gate(self):
        # Room workers inherit the heartbeat's COMMONROOM_HOST, which would replace [share] host here (commonroom#155).
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            os.environ.pop(config.HOST_ENV, None)
            host, guest = Path(tmp) / "game", Path(tmp) / "room"
            host.mkdir(); guest.mkdir()
            (host / "room.toml").write_text('[project]\nname = "game"\n[budget.claude]\nreserve_weekly = 10\nweekly_slack = 14\n'
                                            '[owner]\nnew_work_cap_5h = 60\n')
            (guest / "room.toml").write_text(f'[project]\nname = "room"\n[share]\nhost = "{host.as_posix()}"\nmax = 0.2\n'
                                             '[budget.claude]\nreserve_weekly = 0\n')
            cfg = config.load(guest / "room.toml")
            self.assertEqual(cfg["_host"], "game")
            self.assertEqual(cfg["budget"], config.load(host / "room.toml")["budget"])   # the guest can't loosen it
            self.assertEqual(cfg["budget"]["claude"]["reserve_weekly"], 10)
            self.assertEqual(cfg["owner"]["new_work_cap_5h"], 60)
            (host / "room.toml").unlink()                                   # no host: its own (default) gate
            cfg = config.load(guest / "room.toml")
            self.assertNotIn("_host", cfg)
            self.assertEqual(cfg["budget"]["claude"]["reserve_weekly"], 0)

    def test_a_project_without_share_or_ci_settings_loads_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "room.toml"
            path.write_text('[project]\nname = "game"\n[checks]\ntest = "bash tools/test.sh"\n')
            cfg = config.load(path)
            plain = config.merge(config.DEFAULTS, tomllib.loads(path.read_text()))
            self.assertEqual({k: v for k, v in cfg.items() if not k.startswith("_")}, plain)
            self.assertTrue(config.has_ci(cfg))
            self.assertEqual(cfg["share"], {})

    @unittest.skipUnless(GAMEMIRAGE.is_file(), "gamemirage isn't on this machine")
    def test_gamemirages_config_is_unchanged(self):
        cfg = config.load(GAMEMIRAGE)
        with open(GAMEMIRAGE, "rb") as f:
            plain = config.merge(config.DEFAULTS, tomllib.load(f))
        self.assertEqual({k: v for k, v in cfg.items() if not k.startswith("_")}, plain)
        self.assertNotIn("_host", cfg)
        self.assertTrue(config.has_ci(cfg))
        self.assertEqual(cfg["share"], {})


class NoCiMergeRule(Base):
    def test_tested_on_head_needs_pass_head_sha_and_command(self):
        self.assertTrue(dispatch.tested_on_head(approve(), HEAD, TEST))
        self.assertTrue(dispatch.tested_on_head(approve(command=f"`{TEST} -v`"), HEAD, TEST))
        self.assertFalse(dispatch.tested_on_head("REVIEW: APPROVE\nTests pass.", HEAD, TEST))       # not stated
        self.assertFalse(dispatch.tested_on_head(approve(sha="fedcba9"), HEAD, TEST))                # another commit
        self.assertFalse(dispatch.tested_on_head(approve(verdict="FAIL"), HEAD, TEST))
        self.assertFalse(dispatch.tested_on_head(approve(command="pytest"), HEAD, TEST))             # another command
        self.assertFalse(dispatch.tested_on_head(approve() + f"\nTESTS: FAIL {HEAD[:7]} {TEST}: 1 error", HEAD, TEST))

    def test_tested_approval_merges_without_ci(self):
        board = MergeBoard([pr(5, 40)], {5: detail(approve())})
        actions = self.dispatcher(project_cfg(self.tmp.name), board).housekeeping()
        self.assertEqual(board.merged, [5])
        self.assertTrue(any("merged PR #5" in a for a in actions))

    def test_untested_stale_or_old_approvals_do_not_merge_and_go_back_to_review(self):
        cases = {6: detail("[agent:reviewer] REVIEW: APPROVE\nLGTM, tests pass."),             # no TESTS line
                 7: detail(approve(sha="fedcba9")),                                         # tested an older head
                 8: detail(approve(verdict="FAIL")),
                 9: detail(approve(), reviewed="2026-10-09T11:00:00Z")}                     # before the last commit
        board = MergeBoard([pr(n, 40 + n) for n in cases], cases)
        d = self.dispatcher(project_cfg(self.tmp.name), board)
        d.housekeeping()
        self.assertEqual(board.merged, [])
        for n, dt in cases.items():
            self.assertTrue(d.needs_review(dt), n)
        job, provider, _ = d.pick_review({"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")})
        self.assertEqual((job.kind, job.pr["headRefOid"], provider), ("review", HEAD, "claude"))

    def test_a_builder_quoting_an_approval_and_test_line_does_not_merge(self):
        """commonroom#58: only the reviewer's comment is a verdict, and its TESTS line is the one that counts."""
        quoted = {"createdAt": "2026-10-09T13:30:00Z", "body": "[agent:builder] Expecting " + approve()}
        board = MergeBoard([pr(5, 40)], {5: detail("[agent:reviewer] REVIEW: APPROVE\nLGTM")})
        board.details[5]["comments"].append(quoted)
        d = self.dispatcher(project_cfg(self.tmp.name), board)
        d.housekeeping()
        self.assertEqual(board.merged, [])
        self.assertTrue(d.needs_review(board.details[5]))
        board.details[5]["comments"].insert(1, {"createdAt": "2026-10-09T13:10:00Z", "body": approve()})
        d.housekeeping()                                                   # the reviewer's tested approval still merges
        self.assertEqual(board.merged, [5])

    def test_one_merge_per_pass(self):
        board = MergeBoard([pr(5, 40), pr(6, 41)], {5: detail(approve()), 6: detail(approve())})
        d = self.dispatcher(project_cfg(self.tmp.name), board)
        d.housekeeping()
        self.assertEqual(board.merged, [5])
        board.prs = [pr(6, 41)]
        d.housekeeping()
        self.assertEqual(board.merged, [5, 6])

    def test_a_branch_behind_main_is_updated_then_tested_again(self):
        board = MergeBoard([pr(5, 40)], {5: detail(approve())})
        board.behind[HEAD] = 2
        d = self.dispatcher(project_cfg(self.tmp.name), board)
        d.housekeeping()
        self.assertEqual((board.merged, board.updated), ([], [5]))      # main merged into it first
        merged_main = "fedcba9876543210fedcba9876543210fedcba98"           # the update's new head, after the approval
        board.details[5] = detail(approve(), head=merged_main, committed="2026-10-09T14:00:00Z")
        board.details[5]["commits"].insert(0, {"oid": HEAD, "committedDate": "2026-10-09T12:00:00Z"})
        d.housekeeping()
        self.assertEqual(board.merged, [])                                # the tested sha is no longer the head
        self.assertTrue(d.needs_review(board.details[5]))                 # so the merged tree is reviewed again

    def test_review_skip_needs_a_review_without_ci(self):
        board = MergeBoard([pr(5, 40, labels=["review:skip"])], {5: detail("")})
        d = self.dispatcher(project_cfg(self.tmp.name), board)
        d.housekeeping()
        self.assertEqual(board.merged, [])
        job, _, _ = d.pick_review({"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")})
        self.assertEqual(job.pr["headRefOid"], HEAD)

    def test_reviewer_is_told_to_state_the_test_run_and_untested_approval_is_not_done(self):
        cfg = project_cfg(self.tmp.name)
        board = MergeBoard([], {5: detail("[agent:reviewer] REVIEW: APPROVE\nLGTM", reviewed="2099-01-01T00:00:00Z")})
        d = self.dispatcher(cfg, board)
        job = dispatch.Job("review", "reviewer", pr={"number": 5, "headRefName": "i40-thing"})
        self.assertIn(f"TESTS: PASS <sha> {TEST}", d.prompt(job, None))
        result = type("R", (), {"final_text": "", "errors": []})()
        status, outputs, _ = d.assess(job, None, None, "2026-10-09T00:00:00Z", result)
        self.assertEqual((status, outputs), ("failed", []))
        board.details[5] = detail(approve(), reviewed="2099-01-01T00:00:00Z")
        self.assertEqual(d.assess(job, None, None, "2026-10-09T00:00:00Z", result)[0], "done")

    def test_projects_with_ci_merge_exactly_as_before(self):
        green = [{"conclusion": "SUCCESS"}]
        board = MergeBoard([pr(5, 40), pr(6, 41), pr(7, 42), pr(8, 43, labels=["review:skip"])],
                           {5: detail("REVIEW: APPROVE", checks=green), 6: detail("REVIEW: APPROVE", checks=green),
                            7: detail("REVIEW: APPROVE"), 8: detail("", checks=green)})
        d = self.dispatcher(project_cfg(self.tmp.name, name="game", ci=True), board)
        for _ in range(4):                                   # one merge per pass (commonroom#19), as on main
            actions = d.housekeeping()
            self.assertFalse([a for a in actions or [] if "couldn't list queued CI runs" in str(a)])
            board.prs = [p for p in board.prs if p["number"] not in board.merged]
        self.assertEqual(board.merged, [5, 6, 8])          # no TESTS line needed; CI still required (#7 has none)
        job = dispatch.Job("review", "reviewer", pr={"number": 5, "headRefName": "i40-thing"})
        self.assertNotIn("TESTS:", d.prompt(job, None))
        self.assertEqual(d.update_live_checkout(), [])     # not the room's own checkout


class Share(Base):
    def test_guest_takes_only_its_share_while_the_host_works(self):
        now = time.time()
        cfg = project_cfg(self.tmp.name, share={"max": 0.15, "hours": 24, "idle_minutes": 60}, host="game")
        self.run_for("game", "claude", 300, 400, now)
        self.run_for("game", "claude", 10, 10, now)             # the game worked in the last hour
        self.assertIsNone(dispatch.share_verdict(self.ledger, cfg, "claude", now))        # 0% so far
        self.run_for("commonroom", "claude", 40, 100, now)
        self.assertIsNone(dispatch.share_verdict(self.ledger, cfg, "claude", now))        # 40/350 = 11%
        self.run_for("commonroom", "claude", 30, 50, now)
        held = dispatch.share_verdict(self.ledger, cfg, "claude", now)                   # 70/380 = 18%
        self.assertIn("has had its share", held.reason)
        self.assertNotIn("reserve", held.reason); self.assertNotIn("breaker", held.reason)   # running work isn't stopped
        self.assertIsNone(dispatch.share_verdict(self.ledger, cfg, "codex", now))         # per provider
        self.assertIsNone(dispatch.share_verdict(self.ledger, project_cfg(self.tmp.name, name="game"), "claude", now))

    def test_guest_may_run_while_no_other_project_uses_the_provider(self):
        now = time.time()
        cfg = project_cfg(self.tmp.name, share={"max": 0.15, "hours": 24, "idle_minutes": 60}, host="game")
        self.run_for("commonroom", "claude", 60, 200, now)
        self.run_for("game", "claude", 30, 120, now)            # the game's last run ended 90 min ago
        self.assertIsNone(dispatch.share_verdict(self.ledger, cfg, "claude", now))
        self.ledger.start_run("game", "#3", "builder", "claude", now=now - 60)    # the game is working again
        self.assertIsNotNone(dispatch.share_verdict(self.ledger, cfg, "claude", now))
        cfg["share"]["idle_minutes"] = 0                         # 0: the cap always applies
        self.assertIsNotNone(dispatch.share_verdict(self.ledger, cfg, "claude", now))

    def test_dispatcher_applies_the_share_after_the_shared_gate(self):
        now = time.time()
        self.fresh_readings(now)
        guest = project_cfg(self.tmp.name, share={"max": 0.1}, host="game")
        self.ledger.start_run("game", "#3", "builder", "claude", now=now - 600)
        self.run_for("commonroom", "claude", 60, 70, now)
        allowed = self.dispatcher(guest, MergeBoard([], {})).allowed(now)
        self.assertFalse(allowed["claude"].allowed)
        self.assertIn("game goes first", allowed["claude"].reason)
        self.assertTrue(allowed["codex"].allowed)
        host = self.dispatcher(project_cfg(self.tmp.name, name="game", ci=True), MergeBoard([], {}))
        self.assertTrue(host.allowed(now)["claude"].allowed)       # the host is never capped by a guest

    def test_hosts_room_wide_stops_hold_the_guest_but_not_the_other_way(self):
        now = time.time()
        self.fresh_readings(now)
        guest = project_cfg(self.tmp.name, share={"max": 0.15}, host="game")
        host = project_cfg(self.tmp.name, name="game", ci=True)
        self.ledger.trip("game", "no_output:claude", "the game's own claude runs failed")
        self.assertTrue(dispatch.verdicts(self.ledger, guest, now)["claude"].allowed)    # its own runs judge it
        self.ledger.trip("game", "paused_by_auditor", "owner pause")
        self.assertFalse(any(v.allowed for v in dispatch.verdicts(self.ledger, guest, now).values()))
        self.ledger.clear_breakers("game")
        self.ledger.trip("commonroom", "paused_by_auditor", "room paused")
        self.assertTrue(all(v.allowed for v in dispatch.verdicts(self.ledger, host, now).values()))

    def test_progress_guarantee_charges_a_guest_only_its_share(self):
        now = time.time()
        for t, pct in ((now - 80000, 10), (now - 100, 24)):          # 14% of the week spent plan-wide in 24 h
            self.ledger.record_usage([usage.observation("claude", "seven_day", pct, now + 86400, "test")], now=t)
        self.run_for("game", "claude", 600, 700, now)
        self.run_for("commonroom", "claude", 60, 70, now)            # 60 of 660 minutes: ~1.3% of the week
        self.assertIsNone(dispatch.check_progress(self.ledger, "commonroom", now, attribute=True))
        self.assertEqual(dispatch.check_progress(self.ledger, "game", now)["kind"], "no_progress")   # unchanged


class OwnerAndWorktrees(Base):
    def test_another_projects_workers_are_not_the_owner(self):
        now = time.time()
        projects, game = Path(self.tmp.name) / "projects", Path(self.tmp.name) / "game"
        worker = Path(self.tmp.name) / "commonroom-wt" / "room" / "i31-x"
        folder = projects / usage.session_folder(worker)
        folder.mkdir(parents=True); (folder / "s.jsonl").write_text("{}")
        run = self.ledger.start_run("commonroom", "#31", "builder", "claude", now=now - 300)
        self.ledger.set_worktree(run, worker)
        dirs = self.ledger.worker_dirs(now - 1200)
        self.assertEqual(dirs, [os.path.normcase(os.path.abspath(str(worker)))])   # stored case-folded, as the removal guard does
        usage._owner_cache.clear()
        self.assertEqual(self._owner(str(game), 20, projects_dir=projects, now=now), folder.name)   # before: "the owner"
        usage._owner_cache.clear()
        self.assertIsNone(self._owner(str(game), 20, projects_dir=projects, now=now, exclude=dirs))
        self.ledger.finish_run(run, "done", now=now - 3600)
        self.assertEqual(self.ledger.worker_dirs(now - 1200), [])

    def test_worktree_base_is_configurable_and_defaults_next_to_the_project(self):
        # commonroom#48: the default is no longer the project's .claude/worktrees, which Claude Code manages too.
        root = Path(self.tmp.name) / "gamemirage"
        cfg = project_cfg(root, name="gamemirage")                                 # no `[project] worktrees`, as the game
        d = self.dispatcher(cfg, MergeBoard([], {}))
        self.assertEqual(d.worktree_base(), Path(self.tmp.name) / "gamemirage-wt" / "room")
        self.assertEqual(d.worktree_bases(), [d.worktree_base(), root / ".claude" / "worktrees"])   # live runs at the switch
        cfg["project"]["worktrees"] = "D:/Dev/commonroom-wt/room"
        d = self.dispatcher(cfg, MergeBoard([], {}))
        self.assertEqual(d.worktree_base(), Path("D:/Dev/commonroom-wt/room"))
        self.assertEqual(d.worktree_bases(), [Path("D:/Dev/commonroom-wt/room")])


def touching(*paths, **kw):
    d = detail(approve(), **kw)
    d["files"] = [{"path": p, "additions": 1, "deletions": 0} for p in paths]
    return d


def director(at="2026-10-09T14:00:00Z", verdict="APPROVE", head=HEAD[:7]):
    line = f"HEAD: {head}\n" if head else ""
    return {"createdAt": at, "body": f"[agent:director] REVIEW: {verdict}\n{line}The budget change is what the issue asks for."}


def waits(why):
    return (f"PR #5 {why} and waits for the director's approval: post `[agent:director] REVIEW: APPROVE` "
            f"with the line `HEAD: {HEAD}`")


class DirectorPaths(Base):
    """commonroom#49: budget and safety code merges only with the director's approval."""
    PATHS = ["commonroom/gate.py", "commonroom/config.py", "room.toml"]

    def cfg(self, paths=PATHS, **kw):
        cfg = project_cfg(self.tmp.name, **kw)
        if paths is not None:
            cfg["merge"] = {"director_paths": paths}
        return cfg

    def test_reviewer_approval_alone_does_not_merge_gate_changes_and_the_trigger_names_it(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py", "tests/test_core.py")})
        d = self.dispatcher(self.cfg(), board)
        actions = d.housekeeping()
        self.assertEqual(board.merged, [])
        wait = waits("touches director paths (commonroom/gate.py)")
        self.assertIn(wait, actions)
        self.assertIn(wait, d.director_trigger())
        self.assertFalse(d.needs_review(board.details[5]))              # the reviewer's part is done: no re-review loop

    def test_director_approval_after_the_last_commit_merges(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
        board.details[5]["comments"].append(director())
        d = self.dispatcher(self.cfg(), board)
        self.assertIsNone(d.ready_for_director() or None)
        self.assertFalse(d.needs_review(board.details[5]))              # the director's newer comment hides nothing
        d.housekeeping()
        self.assertEqual(board.merged, [5])

    def test_director_approval_before_the_last_commit_does_not_count(self):
        board = MergeBoard([pr(5, 40)], {5: touching("room.toml", committed="2026-10-09T12:00:00Z")})
        board.details[5]["comments"].insert(0, director(at="2026-10-09T11:00:00Z"))
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual(board.merged, [])

    def test_director_changes_or_a_non_director_approve_does_not_count(self):
        for extra in (director(verdict="CHANGES"),
                      {"createdAt": "2026-10-09T14:00:00Z", "body": "[agent:reviewer] REVIEW: APPROVE (director said ok)"}):
            board = MergeBoard([pr(5, 40)], {5: touching("commonroom/config.py")})
            board.details[5]["comments"].append(extra)
            self.dispatcher(self.cfg(), board).housekeeping()
            self.assertEqual(board.merged, [], extra["body"])

    def test_director_changes_sends_the_pr_back_and_the_trigger_leaves_it_out(self):
        issue = {"number": 40, "state": "OPEN", "labels": [{"name": "status:review"}], "comments": [], "body": ""}
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")}, issues=[issue])
        board.details[5]["comments"].append(director(verdict="CHANGES"))
        d = self.dispatcher(self.cfg(), board)
        actions = d.housekeeping()
        self.assertEqual(board.merged, [])
        self.assertIn("PR #5 needs changes; issue #40 back to ready", actions)
        self.assertIn((40, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), board.labels)
        self.assertEqual(d.ready_for_director(), [])
        self.assertNotIn("director paths", d.director_trigger() or "")
        self.assertFalse(d.needs_review(board.details[5]))              # no reviewer run on a PR the builder now owns

    def test_a_behind_gated_pr_is_updated_before_the_director_is_asked(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
        board.behind[HEAD] = 2
        d = self.dispatcher(self.cfg(), board)
        self.assertEqual(d.ready_for_director(), [])                    # not the director's yet: main moves first
        actions = d.housekeeping()
        self.assertEqual(board.updated, [5])
        self.assertFalse(any("waits for the director" in a for a in actions))
        board.behind[HEAD] = 0                                          # up to date (and re-reviewed): now it's theirs
        self.assertEqual(len(d.ready_for_director()), 1)
        self.assertIn("waits for the director's approval", " ".join(d.housekeeping()))
        self.assertEqual(board.merged, [])

    def test_dry_run_reports_the_director_wait(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
        d = self.dispatcher(self.cfg(), board)
        d.dry_run = True
        actions = d.housekeeping()
        self.assertIn(waits("touches director paths (commonroom/gate.py)"), actions)
        self.assertFalse(any(a.startswith("merged") for a in actions))

    def test_director_approval_does_not_replace_the_reviewers_tested_one(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
        board.details[5]["comments"] = [director()]
        d = self.dispatcher(self.cfg(), board)
        d.housekeeping()
        self.assertEqual(board.merged, [])
        self.assertTrue(d.needs_review(board.details[5]))
        self.assertEqual(d.ready_for_director(), [])                    # nothing for the director until the reviewer approves

    def test_a_pr_touching_only_dispatch_merges_on_the_reviewers_approval(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/dispatch.py", "tests/test_dispatch.py")})
        d = self.dispatcher(self.cfg(), board)
        d.housekeeping()
        self.assertEqual(board.merged, [5])
        self.assertNotIn("director paths", d.director_trigger() or "")

    def test_no_merge_table_behaves_as_before(self):
        for ci in (False, True):
            board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py",
                                                         checks=[{"name": "t", "conclusion": "SUCCESS"}])})
            d = self.dispatcher(self.cfg(paths=None, ci=ci), board)
            self.assertEqual(d.director_paths, [])
            d.housekeeping()
            self.assertEqual(board.merged, [5], ci)
            self.assertEqual(d.ready_for_director(), [])

    def test_with_ci_a_green_approved_gate_change_waits_for_the_director(self):
        green = [{"name": "t", "conclusion": "SUCCESS"}]
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py", checks=green)})
        d = self.dispatcher(self.cfg(ci=True), board)
        d.housekeeping()
        self.assertEqual(board.merged, [])
        self.assertEqual(len(d.ready_for_director()), 1)
        board.details[5]["comments"].append(director())
        d.housekeeping()
        self.assertEqual(board.merged, [5])

    def test_director_approval_naming_an_older_head_does_not_merge_and_the_trigger_still_names_it(self):
        # commonroom#55: a commit made before the approval and pushed after it keeps an older committedDate.
        for head in ("fedcba9", "fedcba9876543210fedcba9876543210fedcba98", HEAD[:6]):
            board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
            board.details[5]["comments"].append(director(head=head))
            d = self.dispatcher(self.cfg(), board)
            self.assertFalse(dispatch.director_approved(board.details[5]), head)
            self.assertIn(waits("touches director paths (commonroom/gate.py)"), d.housekeeping())
            self.assertEqual(board.merged, [], head)
            self.assertIn(waits("touches director paths (commonroom/gate.py)"), d.director_trigger())
            self.assertFalse(d.needs_review(board.details[5]))          # not CHANGES: the reviewer's part stays done

    def test_director_approval_without_a_head_line_does_not_merge(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
        board.details[5]["comments"].append(director(head=None))
        d = self.dispatcher(self.cfg(), board)
        d.housekeeping()
        self.assertEqual(board.merged, [])
        self.assertEqual(len(d.ready_for_director()), 1)                # not approved yet, not sent back

    def test_director_approval_naming_the_full_head_merges(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
        board.details[5]["comments"].append(director(head=HEAD.upper()))
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual(board.merged, [5])

    def test_the_trigger_text_names_the_head_sha(self):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
        trigger = self.dispatcher(self.cfg(), board).director_trigger()
        self.assertIn(f"HEAD: {HEAD}", trigger)
        self.assertIn("[agent:director] REVIEW: APPROVE", trigger)

    def test_the_director_prompt_asks_for_the_head_line(self):
        text = (REPO / ".claude" / "agents" / "director.md").read_text(encoding="utf-8")
        self.assertIn("HEAD: <sha>", text)

    def test_commonrooms_room_toml_names_the_budget_and_safety_files(self):
        cfg = config.load(REPO / "room.toml", follow_host=False)
        self.assertEqual(cfg["merge"]["director_paths"], self.PATHS)


def labelled(*names, **kw):
    d = touching("commonroom/dispatch.py", **kw)
    d["labels"] = [{"name": n} for n in names]
    return d


class NeedsDirectorLabel(Base):
    """commonroom#65: a PR that needs lines in another project's room.toml waits for the director, as director paths do."""
    WAIT = waits("is labelled needs:director")

    def board(self, *names, **kw):
        return MergeBoard([pr(5, 40, labels=names)], {5: labelled(*names, **kw)})

    def test_a_labelled_pr_with_a_tested_approval_waits_and_the_director_is_told(self):
        for name in ("commonroom", "gamemirage"):                       # no director paths needed: the label alone holds it
            board = self.board("needs:director")
            d = self.dispatcher(project_cfg(self.tmp.name, name=name), board)
            self.assertTrue(d.tested(board.details[5]))
            self.assertEqual(d.ready_for_director(), [self.WAIT])
            self.assertIn(self.WAIT, d.housekeeping())
            self.assertEqual(board.merged, [], name)
            self.assertIn(self.WAIT, d.director_trigger())
            self.assertFalse(d.needs_review(board.details[5]))

    def test_the_directors_approval_after_the_last_commit_merges_it(self):
        board = self.board("needs:director")
        board.details[5]["comments"].append(director())
        d = self.dispatcher(project_cfg(self.tmp.name), board)
        self.assertEqual(d.ready_for_director(), [])
        d.housekeeping()
        self.assertEqual(board.merged, [5])

    def test_an_approval_before_the_last_commit_or_a_changes_does_not_merge_it(self):
        for extra, waiting in ((director(at="2026-10-09T11:00:00Z"), [self.WAIT]), (director(verdict="CHANGES"), [])):
            board = self.board("needs:director")
            board.details[5]["comments"].insert(0, extra)
            d = self.dispatcher(project_cfg(self.tmp.name), board)
            d.housekeeping()
            self.assertEqual(board.merged, [], extra)
            self.assertEqual(d.ready_for_director(), waiting)           # a stale approval still waits; CHANGES goes back

    def test_without_the_label_it_merges_on_the_reviewers_approval_as_before(self):
        for name in ("commonroom", "gamemirage"):
            board = self.board("kind:bug")
            d = self.dispatcher(project_cfg(self.tmp.name, name=name), board)
            board.pr_detail = lambda n: self.fail("no director paths and no label: nothing to fetch")
            self.assertEqual(d.ready_for_director(), [])
            board.pr_detail = lambda n: board.details[n]
            d.housekeeping()
            self.assertEqual(board.merged, [5], name)

    def test_the_label_and_director_paths_name_the_files(self):
        board = self.board("needs:director")
        board.details[5]["files"] = [{"path": "room.toml", "additions": 1, "deletions": 0}]
        cfg = project_cfg(self.tmp.name)
        cfg["merge"] = {"director_paths": ["room.toml"]}
        self.assertEqual(self.dispatcher(cfg, board).ready_for_director(),
                         [waits("touches director paths (room.toml)")])

    def test_the_builder_prompt_and_agents_md_say_when_to_add_the_label(self):
        for doc in (REPO / ".claude" / "agents" / "builder.md", REPO / "AGENTS.md"):
            text = doc.read_text(encoding="utf-8")
            self.assertIn("`needs:director`", text, doc.name)
            self.assertIn("another project's `room.toml`", text, doc.name)


class LiveCheckout(Base):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.git = lambda *a, cwd=root: dispatch.git(list(a), cwd)
        self.git("init", "-q", "--bare", "-b", "main", str(root / "origin.git"))
        for name in ("live", "dev"):
            self.git("clone", "-q", str(root / "origin.git"), str(root / name))
            for k, v in (("user.email", "t@t"), ("user.name", "t")):
                self.git("config", k, v, cwd=root / name)
        self.live, self.dev = root / "live", root / "dev"
        self.commit(self.dev, "a.txt"); self.git("push", "-q", "origin", "main", cwd=self.dev)
        self.git("pull", "-q", cwd=self.live)
        self.d = self.dispatcher(project_cfg(self.live), MergeBoard([], {}))
        self.d.room_root = self.live          # pretend this checkout is the one the room runs from

    def commit(self, repo, name):
        (repo / name).write_text(name)
        self.git("add", "-A", cwd=repo); self.git("commit", "-qm", name, cwd=repo)

    def head(self, repo):
        return self.git("rev-parse", "HEAD", cwd=repo)

    def test_fast_forwards_the_rooms_own_checkout_after_a_merge(self):
        self.commit(self.dev, "b.txt"); self.git("push", "-q", "origin", "main", cwd=self.dev)
        board = MergeBoard([pr(5, 40)], {5: detail(approve())})
        self.d.board = board
        actions = self.d.housekeeping()
        self.assertEqual(board.merged, [5])
        self.assertTrue(any("live checkout fast-forwarded" in a for a in actions), actions)
        self.assertEqual(self.head(self.live), self.head(self.dev))
        self.d.room_root = self.dev                                   # someone else's checkout: never touched
        self.assertEqual(self.d.update_live_checkout(), [])

    def test_never_resets_a_diverged_or_switched_checkout(self):
        self.commit(self.dev, "b.txt"); self.git("push", "-q", "origin", "main", cwd=self.dev)
        self.commit(self.live, "local.txt")                           # a local commit: ff impossible
        local = self.head(self.live)
        self.assertIn("FAILED", self.d.update_live_checkout()[0])
        self.assertEqual(self.head(self.live), local)
        self.git("checkout", "-qb", "experiment", cwd=self.live)
        self.assertIn("not main", self.d.update_live_checkout()[0])


class PullSafety(unittest.TestCase):
    def test_room_modules_are_imported_at_start_never_inside_functions(self):
        """The room pulls new code under running dispatchers; a late import would mix old and new modules."""
        for path in (REPO / "commonroom").glob("*.py"):
            if path.name in ("cli.py", "__main__.py"):
                continue   # cli's lazy imports run in short-lived command processes, which load everything new
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(fn):
                    if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("commonroom"):
                        if not (path.name == "dashboard.py" and node.module == "commonroom.cli"):   # already loaded: entry point
                            self.fail(f"{path.name}:{node.lineno} imports {node.module} inside {fn.name}")

    def test_heartbeat_runs_the_rooms_dispatcher_inside_one_block(self):
        lines = (REPO / "heartbeat.cmd").read_text(encoding="utf-8").splitlines()
        start, end = lines.index("("), len(lines) - 1 - lines[::-1].index(")")
        block = lines[start + 1:end]
        self.assertEqual(block[-1].strip(), "exit /b")                 # cmd never reads past the block
        self.assertFalse([l for l in block if l.strip().lower().startswith(("rem", "::"))])
        room = [l for l in block if "heartbeat-commonroom-claude.log" in l]
        self.assertEqual(len(room), 1)
        self.assertIn('/d "%~dp0."', room[0]); self.assertIn("--provider claude", room[0])
        self.assertTrue(room[0].strip().startswith('if exist "%~dp0room.toml" start'))
        self.assertTrue(block[-2].strip().endswith('--provider claude >> "%LOGS%\\heartbeat-claude.log" 2>&1'))



class CarriedStamp(Base):
    """commonroom#168: a director stamp survives merge commits that leave the director paths unchanged."""
    STAMP = "aaaaaaa1111111"
    cfg = DirectorPaths.cfg
    PATHS = DirectorPaths.PATHS

    def chain(self, merges, plain_at=None):
        """Fake compare commits like a real merge-from-main: a first-parent chain from HEAD back to the stamp, with
        single-parent main commits behind each merge's second parent. `plain_at` makes that chain link a plain commit."""
        shas = [HEAD] + [f"{i:040x}" for i in range(1, merges)]
        commits = []
        for i, sha in enumerate(shas):
            first = shas[i + 1] if i + 1 < len(shas) else self.STAMP
            main = f"{i + 100:040x}"
            commits += [{"sha": sha, "parents": [first] if i == plain_at else [first, main]},
                        {"sha": main, "parents": [f"{i + 200:040x}"]}]
        return commits

    def board(self, compare):
        board = MergeBoard([pr(5, 40)], {5: touching("commonroom/gate.py")})
        board.details[5]["comments"].append(director(head=self.STAMP[:7], at="2026-10-09T11:30:00Z"))
        board.compared = []

        def compare_since(base, head):
            board.compared.append((base, head))
            if isinstance(compare, Exception):
                raise compare
            return compare
        board.compare_since = compare_since
        return board

    def test_merges_only_touching_docs_keep_the_stamp_and_the_line_names_it(self):
        board = self.board({"commits": self.chain(2), "files": ["docs/a.md"]})
        actions = self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual(board.merged, [5])
        self.assertIn(f"director stamp {self.STAMP[:7]} carried to {HEAD[:7]}: director paths unchanged", actions)

    def test_a_director_path_in_the_range_drops_the_stamp(self):
        board = self.board({"commits": self.chain(1), "files": ["docs/a.md", "commonroom/config.py"]})
        d = self.dispatcher(self.cfg(), board)
        self.assertIn("waits for the director", " ".join(d.housekeeping()))
        self.assertEqual(board.merged, [])

    def test_a_single_parent_commit_drops_the_stamp(self):
        board = self.board({"commits": self.chain(2, plain_at=0), "files": ["docs/a.md"]})
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual(board.merged, [])

    def test_a_broken_chain_drops_the_stamp(self):
        commits = [c for c in self.chain(2) if c["sha"] != f"{1:040x}"]
        board = self.board({"commits": commits, "files": []})
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual(board.merged, [])

    def test_compare_since_resolves_a_short_stamp_and_asks_for_parent_shas(self):
        calls = []

        def fake_gh(args):
            calls.append(args)
            if "commits/aaaaaaa" in args[1]:
                return {"sha": "a" * 40}
            return {"total": 1, "commits": [{"sha": "b" * 40, "parents": ["a" * 40, "c" * 40]}], "files": ["x"]}
        with mock.patch.object(dispatch.board_mod, "gh", fake_gh):
            found = dispatch.GitHubBoard("o/r").compare_since("aaaaaaa", "b" * 40)
        self.assertEqual(found["files"], ["x"])
        self.assertIn(f"compare/{'a' * 40}...{'b' * 40}", calls[1][1])
        self.assertIn("parents: [.parents[].sha]", calls[1][3])

    def test_a_failing_compare_waits_without_crashing(self):
        board = self.board(RuntimeError("boom"))
        actions = self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual(board.merged, [])
        self.assertTrue(any("waits for the director" in a for a in actions))

    def test_a_newer_director_changes_is_not_carried(self):
        board = self.board({"commits": self.chain(1), "files": []})
        board.details[5]["comments"].append(director(verdict="CHANGES", head=self.STAMP[:7], at="2026-10-09T12:30:00Z"))
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual(board.merged, [])

    def test_one_compare_per_pair_in_a_pass(self):
        board = self.board({"commits": self.chain(1), "files": []})
        d = self.dispatcher(self.cfg(), board)
        d.housekeeping()
        d.ready_for_director()
        self.assertEqual(len(board.compared), 1)

    def test_no_director_paths_never_compares(self):
        board = self.board({"commits": self.chain(1), "files": []})
        board.details[5]["files"] = []
        self.dispatcher(self.cfg(paths=None), board).housekeeping()
        self.assertEqual((board.merged, board.compared), ([5], []))


if __name__ == "__main__":
    unittest.main()
