"""commonroom#31: the room serves itself. Guest share of the plan, the no-CI merge rule, the live checkout update,
and the host project (gamemirage) behaving exactly as before."""
import ast
import os
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

from commonroom import config, dispatch, gate, usage
from commonroom.ledger import Ledger

REPO = Path(__file__).resolve().parent.parent
GAMEMIRAGE = Path("D:/Dev/gamemirage/room.toml")
HEAD = "0123456789abcdef0123456789abcdef01234567"
TEST = "python -m unittest discover -s tests"


class MergeBoard:
    def __init__(self, prs, details, issues=()):
        self.prs, self.details, self.issues, self.merged, self.labels = prs, details, list(issues), [], []

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
        with tempfile.TemporaryDirectory() as tmp:
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

    def test_one_merge_per_pass(self):
        board = MergeBoard([pr(5, 40), pr(6, 41)], {5: detail(approve()), 6: detail(approve())})
        d = self.dispatcher(project_cfg(self.tmp.name), board)
        actions = d.housekeeping()
        self.assertEqual(board.merged, [5])
        self.assertTrue(any("PR #6 is approved; it merges next pass" in a for a in actions))
        board.prs = [pr(6, 41)]
        d.housekeeping()
        self.assertEqual(board.merged, [5, 6])

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
        d.housekeeping()
        self.assertEqual(board.merged, [5, 6, 8])          # no TESTS line needed, several per pass, CI still required
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
        self.ledger.set_cwd(run, worker)
        dirs = self.ledger.worker_dirs(now - 1200)
        self.assertEqual(dirs, [str(worker)])
        usage._owner_cache.clear()
        self.assertEqual(self._owner(str(game), 20, projects_dir=projects, now=now), folder.name)   # before: "the owner"
        usage._owner_cache.clear()
        self.assertIsNone(self._owner(str(game), 20, projects_dir=projects, now=now, exclude=dirs))
        self.ledger.finish_run(run, "done", now=now - 3600)
        self.assertEqual(self.ledger.worker_dirs(now - 1200), [])

    def test_worktree_base_is_configurable_and_defaults_as_before(self):
        cfg = project_cfg(self.tmp.name)
        d = self.dispatcher(cfg, MergeBoard([], {}))
        self.assertEqual(d.worktree_base(), Path(self.tmp.name) / ".claude" / "worktrees")
        cfg["project"]["worktrees"] = "D:/Dev/commonroom-wt/room"
        self.assertEqual(self.dispatcher(cfg, MergeBoard([], {})).worktree_base(), Path("D:/Dev/commonroom-wt/room"))


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


if __name__ == "__main__":
    unittest.main()
