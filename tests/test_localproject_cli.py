"""commonroom#651: a non-code project on a local board, walked through the commands a person would use, with a fake worker."""
import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import board, cli, config, dispatch, gate, page, registry, sessions, usage, workers
from commonroom.ledger import Ledger
from commonroom.localboard import LocalBoard

TOML = ('[project]\nname = "notes"\nboard = "local"\nmission = "Answer neutral questions."\n'
        '[goal]\nmilestone = "Questions"\n[roles]\nresearcher = ["claude:haiku"]\n[director]\nsteer_every_hours = 0\n')


class LocalProjectCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.root = Path(self.tmp.name).resolve()
        self.home, self.proj = root / "home", root / "proj"      # the project folder is not a git repo
        (self.proj / ".claude" / "agents").mkdir(parents=True)
        (self.proj / ".claude" / "agents" / "researcher.md").write_text("---\nname: researcher\n---\nYou research.\n", encoding="utf-8")
        (self.proj / "room.toml").write_text(TOML, encoding="utf-8")
        self.home.mkdir()
        (root / "transcripts").mkdir()      # never the real ~/.claude/projects
        patcher = mock.patch.object(sessions, "default_dir", return_value=root / "transcripts")
        patcher.start(); self.addCleanup(patcher.stop)
        for patcher in (mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)}),
                        mock.patch.object(board, "gh", side_effect=AssertionError("gh was called")),
                        mock.patch.object(dispatch.Dispatcher, "refresh_readings", lambda self, now: None),
                        mock.patch.object(dispatch.Dispatcher, "allowed", lambda self, now: {"claude": gate.Verdict(True, "ok")})):
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cli.time, "sleep")     # `--until-idle` waits 5 s between jobs
        patcher.start(); self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self.proj); self.addCleanup(os.chdir, cwd)

    def room(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def board(self):
        return LocalBoard(self.home / "projects" / "notes")

    def state(self):
        ledger = Ledger(self.home / "room.sqlite3", synchronous="OFF")
        self.addCleanup(ledger.close)
        now = time.time()
        for at, pct in ((now - 7200, 10), (now - 3600, 12)):
            ledger.record_usage([usage.observation("claude", "seven_day", pct, now + 3.5 * 86400, "test")], now=at)
        cfg = config.load(self.proj / "room.toml")
        return page.page_state(ledger, cfg, now, reg=registry.load(registry.default_path()), transcripts=self.root / "transcripts")

    def register(self):
        code, out, err = self.room("projects", "add", "notes", str(self.proj), "--share", "10")
        self.assertEqual(code, 0, err)

    def test_a_research_item_runs_from_registration_to_the_digest(self):
        self.register()
        code, out, err = self.room("add", "notes", "What is the tallest lighthouse?", "--body", "One paragraph.", "--role", "researcher")
        self.assertEqual((code, out.strip()), (0, "notes #1: added"), err)
        self.assertIn("#1 role:researcher status:ready What is the tallest lighthouse?", self.room("items", "notes")[1])
        prompts = []
        def worker(provider, role, prompt, cwd, *a, **kw):
            prompts.append(prompt)
            return workers.WorkerResult(ok=True, final_text="The answer is 42 m.\nSTATUS: DONE")
        with mock.patch.object(dispatch, "run_worker", side_effect=worker):
            self.assertEqual(self.room("run", "--until-idle")[0], 0)
        self.assertEqual(len(prompts), 1)
        self.assertIn("What is the tallest lighthouse?", prompts[0])
        self.assertEqual(self.board().issue_state(1), "CLOSED")
        self.assertIn("The answer is 42 m.", " ".join(c["body"] for c in self.board().issue_comments(1, 0)))
        self.assertEqual(self.room("items", "notes")[1].strip(), "")
        self.assertIn("#1 role:researcher What is the tallest lighthouse? [closed]", self.room("items", "notes", "--all")[1])
        section = next(s for s in self.state()["projects"] if s["name"] == "notes")
        self.assertEqual((section["landed_7d"], section["landed_recent"]), (1, ["#1"]), section)
        digest = self.room("digest", "notes")[1]
        self.assertNotIn("nothing run or landed", digest)

    def test_an_owner_task_shows_in_one_line_and_a_comment_reaches_the_next_prompt(self):
        self.register()
        self.room("add", "notes", "Pick the source list", "--body", "Due: 2999-01-01", "--owner")
        waiting = self.state()["waiting"]
        self.assertEqual([(w["project"], w["number"], w["title"]) for w in waiting], [("notes", 1, "Pick the source list")])
        digest = self.room("digest", "notes")[1]
        self.assertEqual([l for l in digest.splitlines() if "Pick the source list" in l], ["- #1 Pick the source list, due 2999-01-01"])
        self.room("add", "notes", "Second question", "--role", "researcher")
        self.assertEqual(self.room("comment", "notes", "2", "Use only primary sources.")[0], 0)
        prompts = []
        def worker(provider, role, prompt, cwd, *a, **kw):
            prompts.append(prompt)
            return workers.WorkerResult(ok=True, final_text="Done.\nSTATUS: DONE")
        with mock.patch.object(dispatch, "run_worker", side_effect=worker):
            self.room("run", "--until-idle")
        self.assertIn("Use only primary sources.", prompts[0])
        self.assertEqual(self.board().issue_state(1), "OPEN")    # the owner's task is never picked

    def test_add_refuses_without_exactly_one_of_role_and_owner(self):
        self.register()
        for argv in (("add", "notes", "t"), ("add", "notes", "t", "--role", "r", "--owner"), ("add", "notes", " ", "--owner")):
            self.assertEqual(self.room(*argv)[0], 2, argv)
        self.assertEqual(self.board().all_issues(), [])

    def test_a_github_project_trusts_the_same_logins_as_before(self):
        cfg = config.merge(config.DEFAULTS, {"project": {"name": "g", "repo": "me/g", "trusted": ["pal"]}})
        self.assertEqual(config.trusted(cfg), {"me", "pal"})
        self.assertEqual(config.trusted(config.merge(config.DEFAULTS, {"project": {"board": "local"}})), {"room", "owner"})


if __name__ == "__main__":
    unittest.main()
