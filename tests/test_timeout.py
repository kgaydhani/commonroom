"""The run time limit: told to the worker, and said on the issue when a build runs out of it (commonroom#117)."""
import tempfile
import unittest
from pathlib import Path

from commonroom import dispatch, workers
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with


class TimeoutNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        root = Path(self.tmp.name)
        (root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "agents" / "builder.md").write_text("---\nmodel: sonnet\n---\nBuild.\n")
        self.issue = {"number": 7, "title": "t", "labels": [], "state": "OPEN"}

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def build(self, errors, commits, final_text=""):
        board = FakeBoard([self.issue])
        board.pr_for_branch = lambda branch: None
        root = Path(self.tmp.name)
        d = dispatch.Dispatcher(cfg_with(root), self.ledger, board=board, log=lambda *_: None)
        d.prepare_worktree = lambda job, provider=None: (root / "wt", "i7-x")
        d.remote_head = lambda branch: None
        d.prompt = lambda job, branch: "build"
        d.preserve_work = lambda job, worktree, branch: None
        d.pushed_commits = lambda branch: commits
        d.cleanup_worktree = lambda path, run_id=None: None
        seen = {}
        orig_git, orig_run = dispatch.git, dispatch.run_worker

        def fake_run(provider, role, prompt, *a, **k):
            seen.update(prompt=prompt, timeout=k.get("timeout"))
            return workers.WorkerResult(ok=not errors, final_text=final_text, errors=errors)
        dispatch.git, dispatch.run_worker = lambda args, cwd: "", fake_run
        try:
            d.run_job(dispatch.Job("build", "builder", issue=self.issue), "claude", "sonnet")
        finally:
            dispatch.git, dispatch.run_worker = orig_git, orig_run
        return board, seen

    def test_the_prompt_states_the_timeout_run_worker_gets(self):
        board, seen = self.build([], 0)
        self.assertEqual(seen["timeout"], workers.RUN_TIMEOUT)
        self.assertIn("The room ends this run after 60 minutes.", seen["prompt"])
        self.assertIn("By minute 45, push what you have", seen["prompt"])
        orig = workers.RUN_TIMEOUT
        workers.RUN_TIMEOUT = 1800
        try:
            board, seen = self.build([], 0)
        finally:
            workers.RUN_TIMEOUT = orig
        self.assertEqual(seen["timeout"], 1800)
        self.assertIn("ends this run after 30 minutes.", seen["prompt"])
        self.assertIn("By minute 15, push", seen["prompt"])

    def test_a_timed_out_build_with_no_commits_says_nothing_was_preserved(self):
        board, _ = self.build(["timed out after 3600s"], 0)
        notes = [b for n, b in board.comments if "time limit" in b]
        self.assertEqual(len(notes), 1)
        self.assertIn("60-minute time limit", notes[0])
        self.assertIn("Nothing was committed", notes[0])

    def test_a_timed_out_build_with_commits_names_the_branch(self):
        board, _ = self.build(["timed out after 3600s"], 2)
        notes = [b for n, b in board.comments if "time limit" in b]
        self.assertEqual(len(notes), 1)
        self.assertIn("`i7-x`", notes[0])
        self.assertNotIn("Nothing was committed", notes[0])

    def test_a_run_that_does_not_time_out_posts_no_timeout_comment(self):
        board, _ = self.build(["some other error"], 0)
        self.assertFalse([b for n, b in getattr(board, "comments", []) if "time limit" in b])


if __name__ == "__main__":
    unittest.main()
