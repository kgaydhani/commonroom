"""`[codex] scratch_env`: Codex workers get named variables pointed inside their worktree (commonroom#81).
The worker start is captured, never run."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from commonroom import dispatch, workers
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class ScratchEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.ledger = Ledger(self.base / "room.sqlite3")
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "a.txt").write_text("a")
        git("add", "a.txt", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a", cwd=self.repo)
        self.wt = self.base / "wt" / "i81-x"
        git("worktree", "add", "-q", "-b", "i81-x", str(self.wt), "main", cwd=self.repo)
        self.orig = workers.subprocess.Popen
        self.addCleanup(self.cleanup)

    def cleanup(self):
        workers.subprocess.Popen = self.orig
        shutil.rmtree(workers.worker_tmp(self.wt), ignore_errors=True)
        self.ledger.close()
        self.tmp.cleanup()

    def start(self, provider, **kwargs) -> dict:
        """The environment the worker would have started with."""
        seen = {}
        def fake_popen(cmd, **kw):
            if "env" not in kw:
                return self.orig(cmd, **kw)                       # the room's own git calls run for real
            seen.update(kw["env"]); raise RuntimeError("not starting a real worker")
        workers.subprocess.Popen = fake_popen
        with self.assertRaises(RuntimeError):
            workers.run_worker(provider, workers.Role("builder", None, [], "x"), "y", self.wt, self.ledger,
                               self.base / "logs", **kwargs)
        return seen

    def test_a_codex_worker_gets_appdata_inside_its_worktree_and_git_ignores_it(self):
        env = self.start("codex", scratch_env=["APPDATA"])
        self.assertEqual(Path(env["APPDATA"]), self.wt / ".scratch" / "appdata")
        self.assertTrue((self.wt / ".scratch" / "appdata").is_dir())
        (self.wt / ".scratch" / "appdata" / "Godot").mkdir()
        (self.wt / ".scratch" / "appdata" / "Godot" / "log.txt").write_text("x")   # what the run would leave
        self.assertEqual(git("status", "--porcelain", cwd=self.wt), "")
        self.assertEqual(git("status", "--porcelain", cwd=self.repo), "")
        self.start("codex", scratch_env=["APPDATA"])                                  # a second run adds no duplicate
        exclude = self.repo / ".git" / "info" / "exclude"
        self.assertEqual(exclude.read_text().splitlines().count("/.scratch/"), 1)
        for name in ("LOCALAPPDATA", "USERPROFILE"):                                  # Codex's own config stays put
            self.assertEqual(env.get(name), os.environ.get(name))
        # gh's login stays reachable: GH_CONFIG_DIR points at the real %APPDATA%\GitHub CLI (gamemirage#428)
        if os.environ.get("APPDATA") and not os.environ.get("GH_CONFIG_DIR"):
            self.assertEqual(Path(env["GH_CONFIG_DIR"]), Path(os.environ["APPDATA"]) / "GitHub CLI")

    def test_claude_workers_and_codex_without_scratch_env_keep_appdata(self):
        for provider, scratch in (("claude", ["APPDATA"]), ("codex", None), ("codex", [])):
            env = self.start(provider, scratch_env=scratch)
            self.assertEqual(env.get("APPDATA"), os.environ.get("APPDATA"), (provider, scratch))
            self.assertEqual(env.get("GH_CONFIG_DIR"), os.environ.get("GH_CONFIG_DIR"), (provider, scratch))
        self.assertFalse((self.wt / ".scratch" / "appdata").exists())

    def test_a_codex_worker_temp_is_inside_its_worktree(self):
        env = workers.worker_env("codex", self.wt)
        tmp = self.wt / ".scratch" / "tmp"
        for name in ("TEMP", "TMP", "TMPDIR"):
            self.assertEqual(Path(env[name]), tmp)
        self.assertTrue(tmp.is_dir())
        self.assertTrue(tmp.resolve().is_relative_to(self.wt.resolve()))
        self.assertEqual(git("status", "--porcelain", cwd=self.wt), "")

    def test_a_claude_worker_temp_stays_worker_tmp(self):
        env = workers.worker_env("claude", self.wt, ["APPDATA"])
        for name in ("TEMP", "TMP", "TMPDIR"):
            self.assertEqual(Path(env[name]), workers.worker_tmp(self.wt))
        self.assertFalse((self.wt / ".scratch").exists())

    def test_a_codex_worker_with_scratch_env_gets_temp_and_appdata(self):
        env = workers.worker_env("codex", self.wt, ["APPDATA"])
        self.assertEqual(Path(env["TEMP"]), self.wt / ".scratch" / "tmp")
        self.assertEqual(Path(env["APPDATA"]), self.wt / ".scratch" / "appdata")
        self.assertTrue((self.wt / ".scratch" / "appdata").is_dir())

    def test_the_dispatcher_passes_the_projects_list_and_nothing_by_default(self):
        root = self.base / "proj"
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".claude" / "agents" / "director.md").write_text("---\nmodel: opus\n---\nDirect.\n")

        def passed(cfg):
            seen = {}
            d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
            d.prepare_worktree = lambda job, provider=None: (self.wt, None)
            d.remote_head = lambda branch: None
            d.prompt = lambda job, branch: "direct"
            d.preserve_work = lambda job, worktree, branch: None
            d.cleanup_worktree = lambda path, run_id=None: None
            orig_git, orig_run = dispatch.git, dispatch.run_worker
            dispatch.git = lambda args, cwd: ""
            dispatch.run_worker = lambda *a, **k: (seen.update(k), workers.WorkerResult(ok=True, final_text="x"))[1]
            try:
                d.run_job(dispatch.Job("direct", "director", reason="steer"), "codex", None)
            finally:
                dispatch.git, dispatch.run_worker = orig_git, orig_run
            return seen["scratch_env"]

        self.assertIsNone(passed(cfg_with(root)))                                     # gamemirage today: unchanged
        cfg = cfg_with(root); cfg["codex"] = {"scratch_env": ["APPDATA"]}
        self.assertEqual(passed(cfg), ["APPDATA"])


if __name__ == "__main__":
    unittest.main()
