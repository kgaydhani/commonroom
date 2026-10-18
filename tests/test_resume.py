"""Resume a Claude build or review the room stopped mid-run (commonroom#142)."""
import tempfile
import time
import unittest
from pathlib import Path

from commonroom import dispatch, workers
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with

SESSION = "59facac2-aaaa"


class SessionFile(unittest.TestCase):
    def test_maps_a_worktree_to_its_claude_folder(self):
        got = dispatch.session_file(r"d:\dev\gamemirage-wt\room\i660-x", "s1", Path("P"))
        self.assertEqual(got, Path("P") / "d--dev-gamemirage-wt-room-i660-x" / "s1.jsonl")


class Resume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3")
        for role in ("builder", "reviewer"):
            (self.root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
            (self.root / ".claude" / "agents" / f"{role}.md").write_text("---\nmodel: sonnet\n---\nX.\n")
        self.projects = self.root / "projects"
        self.issue = {"number": 7, "title": "t", "labels": [], "state": "OPEN"}
        self.pr = {"number": 9, "headRefName": "i7-x", "headRefOid": "abc1234def"}

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def earlier(self, d, task, role="builder", provider="claude", status="stopped", ago=3600, wt=None, session=SESSION,
                note="stopped by the room: 5-hour reserve", file=True):
        wt = wt or self.wt(role)
        rid = self.ledger.start_run(d.project, task, role, provider, now=time.time() - ago - 60)
        self.ledger.finish_run(rid, status, note=note, session_id=session, now=time.time() - ago)
        self.ledger.set_worktree(rid, wt)
        if file and session:
            f = dispatch.session_file(wt, session, self.projects)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}")
        return rid

    def wt(self, role):
        return self.root / ("review-9" if role == "reviewer" else "i7-x")

    def run_job(self, setup, kind="build", provider="claude"):
        board = FakeBoard([self.issue])
        board.pr_for_branch = lambda branch: None
        d = dispatch.Dispatcher(cfg_with(self.root), self.ledger, board=board, log=lambda *_: None)
        d.claude_projects = self.projects
        d.prepare_worktree = lambda job, provider=None: (self.wt(job.role), "i7-x")
        d.remote_head = lambda branch: "feed000"
        d.prompt = lambda job, branch: "FULL PROMPT"
        d.assess = lambda job, branch, before, started, result: ("done", [], "ok")
        d.preserve_work = lambda job, worktree, branch: None
        d.pushed_commits = lambda branch: 0
        d.cleanup_worktree = lambda path, run_id=None: None
        setup(d)
        seen = {}
        orig_git, orig_run = dispatch.git, dispatch.run_worker

        def fake_run(prov, role, prompt, *a, **k):
            seen.update(prompt=prompt, resume=k.get("resume_session"))
            return workers.WorkerResult(ok=True, final_text="x", session_id="new")
        dispatch.git, dispatch.run_worker = lambda args, cwd: "", fake_run
        try:
            if kind == "build":
                job = dispatch.Job("build", "builder", issue=self.issue)
            else:
                job = dispatch.Job("review", "reviewer", pr=self.pr)
            d.run_job(job, provider, "sonnet")
        finally:
            dispatch.git, dispatch.run_worker = orig_git, orig_run
        return seen

    def test_a_stopped_build_resumes_with_a_continuation_prompt(self):
        seen = self.run_job(lambda d: self.earlier(d, "#7"))
        self.assertEqual(seen["resume"], SESSION)
        self.assertNotIn("FULL PROMPT", seen["prompt"])
        self.assertIn("5-hour reserve", seen["prompt"])
        self.assertIn("WIP commit", seen["prompt"])
        self.assertIn("The room ends this run after 60 minutes.", seen["prompt"])

    def test_the_run_records_the_resume(self):
        self.run_job(lambda d: self.earlier(d, "#7"))
        self.assertIn(f"resumed session {SESSION}", self.ledger.recent_runs(limit=1)[0]["note"])

    def test_a_stopped_review_resumes_with_the_head_sha(self):
        seen = self.run_job(lambda d: self.earlier(d, "PR #9", role="reviewer"), kind="review")
        self.assertEqual(seen["resume"], SESSION)
        self.assertIn("abc1234def", seen["prompt"])
        self.assertIn("5-hour reserve", seen["prompt"])
        self.assertIn("The room ends this run after 60 minutes.", seen["prompt"])

    def assert_fresh(self, setup, kind="build", task="#7", provider="claude"):
        seen = self.run_job(setup, kind=kind, provider=provider)
        self.assertIsNone(seen["resume"])
        self.assertIn("FULL PROMPT", seen["prompt"])

    def test_fresh_when_the_run_is_too_old(self):
        self.assert_fresh(lambda d: self.earlier(d, "#7", ago=13 * 3600))

    def test_fresh_when_the_latest_run_finished(self):
        for status in ("done", "failed"):
            self.assert_fresh(lambda d: self.earlier(d, "#7", status=status))

    def test_fresh_for_codex(self):
        self.assert_fresh(lambda d: self.earlier(d, "#7", provider="codex"))

    def test_fresh_when_the_worktree_differs(self):
        self.assert_fresh(lambda d: self.earlier(d, "#7", wt=self.root / "i7-x-123"))

    def test_fresh_when_the_session_file_is_missing(self):
        self.assert_fresh(lambda d: self.earlier(d, "#7", file=False))

    def test_fresh_when_the_latest_run_has_no_session(self):
        self.assert_fresh(lambda d: self.earlier(d, "#7", session=None))

    def test_fresh_when_a_later_run_followed(self):
        def setup(d):
            self.earlier(d, "#7", ago=7200)
            self.earlier(d, "#7", status="failed", ago=3600, session="other")
        self.assert_fresh(setup)

    def test_fresh_after_three_stopped_runs_in_a_row(self):
        def setup(d):
            self.earlier(d, "#7", ago=9000, session="s1")
            self.earlier(d, "#7", ago=7000, status="limited", session="s2")
            self.earlier(d, "#7", ago=3600, session="s3")
        self.assert_fresh(setup)

    def test_two_stopped_runs_still_resume(self):
        def setup(d):
            self.earlier(d, "#7", ago=7000, session="s2")
            self.earlier(d, "#7", ago=3600, session="s3")
        self.assertEqual(self.run_job(setup)["resume"], "s3")

    def test_a_stopped_run_of_another_role_is_not_resumed(self):
        self.assert_fresh(lambda d: self.earlier(d, "#7", role="reviewer", wt=self.wt("builder")))


if __name__ == "__main__":
    unittest.main()
