"""commonroom#455: `[project] board = "local"` runs a project's items on files, with no GitHub repo."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import board as board_mod, config, dispatch, gate, metrics, workers
from commonroom.ledger import Ledger
from commonroom.localboard import LocalBoard


def cfg_for(root, project):
    cfg = config.merge(config.DEFAULTS, {"project": project, "goal": {"milestone": "M0"}, "roles": {"writer": ["claude:sonnet"]}})
    cfg["_root"] = str(root)
    cfg["min_free_gb"] = {"room": 0, "project": 0}
    cfg["director"]["steer_every_hours"] = 0
    return cfg


class LocalProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3", synchronous="OFF")
        agents = self.root / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "writer.md").write_text("---\nname: writer\n---\nYou are the writer.\n", encoding="utf-8")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def write_config(self, text):
        path = self.root / "room.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_local_board_without_repo_loads_and_unknown_board_is_refused(self):
        cfg = config.load(self.write_config('[project]\nname = "demo"\nboard = "local"\n'))
        self.assertEqual((config.board_kind(cfg), cfg["project"]["repo"]), ("local", None))
        with self.assertRaises(config.ConfigError) as ctx:
            config.load(self.write_config('[project]\nname = "demo"\nboard = "jira"\n'))
        self.assertIn("[project] board", str(ctx.exception))

    def test_no_board_key_still_builds_github_board(self):
        cfg = config.load(self.write_config('[project]\nname = "demo"\nrepo = "o/demo"\n'))
        self.assertEqual(config.board_kind(cfg), "github")
        d = dispatch.Dispatcher(cfg, self.ledger, log=lambda *_: None)
        self.assertIsInstance(d.board.inner, dispatch.GitHubBoard)
        self.assertEqual(d.board.inner.repo, "o/demo")

    def test_local_role_item_is_picked_and_closed_by_done_without_gh(self):
        cfg = config.load(self.write_config('[project]\nname = "demo"\nboard = "local"\n[goal]\nmilestone = "M0"\n'
                                            '[roles]\nwriter = ["claude:sonnet"]\n[director]\nsteer_every_hours = 0\n'))
        with mock.patch.object(board_mod, "gh", side_effect=AssertionError("gh was called")):
            d = dispatch.Dispatcher(cfg, self.ledger, log=lambda *_: None)
            self.assertIsInstance(d.board.inner, LocalBoard)
            self.assertEqual(d.board.open_prs(), [])
            n = d.board.inner.create_issue("Write the note", "Body.", labels=["status:ready", "role:writer"], milestone="M0")
            allowed = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}
            job, provider, _ = d.pick(allowed)
            self.assertEqual((job.kind, job.role, job.issue["number"], provider), ("role", "writer", n, "claude"))
            status, outputs, _ = d.assess(job, None, None, "", workers.WorkerResult(ok=True, final_text="Written.\nSTATUS: DONE"))
            self.assertEqual((status, outputs), ("done", [{"kind": "writer", "ref": f"#{n} closed"}]))
            self.assertEqual(d.board.inner.issue_state(n), "CLOSED")
            self.assertEqual(d.board.open_issues("M0"), [])
        self.assertTrue((self.root / "projects" / "demo" / "board" / "items" / str(n)).is_dir())

    def test_local_role_prompt_carries_the_item_and_names_no_gh(self):
        cfg = config.load(self.write_config('[project]\nname = "demo"\nboard = "local"\n[goal]\nmilestone = "M0"\n'))
        d = dispatch.Dispatcher(cfg, self.ledger, log=lambda *_: None)
        n = d.board.inner.create_issue("Write the note", "Cover the {{repo}} cases.", labels=["role:writer"], milestone="M0")
        d.board.inner.comment(n, "Mind the deadline.")
        job = dispatch.Job("role", "writer", issue=d.board.inner.all_issues()[0])
        text = d.prompt(job, None)
        for part in ("Write the note", "Cover the {{repo}} cases.", "Mind the deadline.", "STATUS: DONE"):
            self.assertIn(part, text)
        for part in ("--repo", "gh issue", "None", "kind:bug"):
            self.assertNotIn(part, text)

    def test_landings_on_a_local_board_are_closed_role_items_only(self):
        now = time.time()
        closed = self.ledger.start_run("demo", "#1", "writer", "claude")
        self.ledger.finish_run(closed, "done", outputs=[{"kind": "writer", "ref": "#1 closed"}])
        blocked = self.ledger.start_run("demo", "#2", "writer", "claude")
        self.ledger.finish_run(blocked, "blocked", outputs=[{"kind": "blocked-report", "ref": "#2 blocked"}])
        merge = self.ledger.start_run("demo", "#3", "merge", "none")
        self.ledger.finish_run(merge, "done", outputs=[{"kind": "merge", "ref": "PR #3"}])
        rows = self.ledger.runs_since("demo", now - 3600)
        self.assertEqual([r["task"] for r in metrics.landings(rows, now - 3600, now + 60, True)], ["#1"])
        self.assertEqual([r["task"] for r in metrics.landings(rows, now - 3600, now + 60)], ["#3"])   # GitHub: unchanged

    def test_dispatcher_counts_local_landings_for_progress(self):
        cfg = cfg_for(self.root, {"name": "demo", "board": "local"})
        d = dispatch.Dispatcher(cfg, self.ledger, log=lambda *_: None)
        run = self.ledger.start_run("demo", "#1", "writer", "claude")
        self.ledger.finish_run(run, "done", outputs=[{"kind": "writer", "ref": "#1 closed"}])
        self.assertEqual(d.landed_count(time.time()), 1)

    def local_dispatcher(self, repo: bool):
        if repo:
            for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start"]):
                subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        toml = '[project]\nname = "demo"\nboard = "local"\n[goal]\nmilestone = "M0"\n[roles]\nwriter = ["claude:sonnet"]\n[director]\nsteer_every_hours = 0\n'
        cfg = config.load(self.write_config(toml))
        d = dispatch.Dispatcher(cfg, self.ledger, log=lambda *_: None)
        d.refresh_readings = lambda now: None
        d.allowed = lambda now: {"claude": gate.Verdict(True, "ok")}
        return d

    def run_local_item(self, repo: bool):
        d = self.local_dispatcher(repo)
        n = d.board.inner.create_issue("Write the note", "Body.", labels=["status:ready", "role:writer"], milestone="M0")
        seen = []
        def worker(provider, role, prompt, cwd, *a, **kw):
            seen.append(Path(cwd))
            self.assertTrue(Path(cwd).is_dir())
            return workers.WorkerResult(ok=True, final_text="Written.\nSTATUS: DONE")
        with mock.patch.object(board_mod, "gh", side_effect=AssertionError("gh was called")), \
                mock.patch.object(dispatch, "run_worker", side_effect=worker):
            report = d.run_once()
        self.assertEqual(report["job"]["status"], "done", report)
        self.assertEqual(d.board.inner.issue_state(n), "CLOSED")
        run = [r for r in self.ledger.runs_since("demo", 0) if r["task"] == f"#{n}"]
        self.assertEqual([r["status"] for r in run], ["done"])
        self.assertFalse(seen[0].exists())   # cleaned up
        return seen[0]

    def test_run_once_runs_a_local_item_in_a_git_project_with_no_origin(self):
        where = self.run_local_item(repo=True)
        self.assertNotEqual(where, self.root)

    def test_run_once_runs_a_local_item_in_a_folder_that_is_not_a_git_repo(self):
        where = self.run_local_item(repo=False)
        self.assertIn((self.root / "projects" / "demo" / "work").resolve(), where.resolve().parents)

    def test_a_local_board_answers_the_pr_side_with_nothing(self):
        board = LocalBoard(self.root / "b")
        self.assertEqual(board.main_checks(), [])
        self.assertEqual(board.behind_main({}), 0)
        for name, args in (("merge", (1,)), ("update_branch", (1,)), ("close_pr", (1, "x")), ("label_pr", (1, "x"))):
            self.assertIsNone(getattr(board, name)(*args), name)
        self.assertEqual((board.compare_since("a", "b"), board.own_patches("a", "b")), ({}, {}))

    def test_housekeeping_on_a_local_board_logs_no_main_checks_error(self):
        d = self.local_dispatcher(repo=False)
        d.ci = True
        lines = []
        d.log = lines.append
        self.assertFalse(d.main_red([], lines))
        self.assertFalse(any("main's CI" in l for l in lines), lines)

    def test_a_github_board_still_fetches_before_a_run(self):
        cfg = cfg_for(self.root, {"name": "demo", "repo": "o/demo"})
        d = dispatch.Dispatcher(cfg, self.ledger, board=mock.Mock(), log=lambda *_: None)
        calls = []
        with mock.patch.object(dispatch, "git", side_effect=lambda args, cwd: calls.append(args) or ""), \
                mock.patch.object(d, "prepare_worktree", side_effect=RuntimeError("stop")), mock.patch.object(d, "room_error"):
            job = dispatch.Job("role", "writer", issue={"number": 1, "title": "t", "labels": []})
            with self.assertRaises(RuntimeError):
                d.run_claimed(job, None, "claude", None, None, self.ledger.start_run("demo", "#1", "writer", "claude"), "#1")
        self.assertIn(["fetch", "--quiet", "origin"], calls)


if __name__ == "__main__":
    unittest.main()
