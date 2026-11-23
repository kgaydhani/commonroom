"""The dispatcher runs the variant arm of a trial and records each run's arm (commonroom#352)."""
import tempfile
import unittest
from pathlib import Path

from commonroom import dispatch, trials, workers
from commonroom.ledger import Ledger

import test_resume
from test_dispatch import FakeBoard, cfg_with


def declare(folder, body):
    (folder / "trials.toml").write_text(body, encoding="utf-8")
    (folder / "trials").mkdir(exist_ok=True)
    (folder / "trials" / "review-v2.md").write_text("VARIANT REVIEW", encoding="utf-8")


PROMPT = """[[trial]]
name = "t1"
area = "review"
started = "2020-01-01"
share = 0.5
measure = "review_runs_per_landing"
min_landings = 3
max_days = 10
[trial.variant]
prompts = { review = "trials/review-v2.md" }
"""
POLICY = PROMPT.replace('prompts = { review = "trials/review-v2.md" }', "policy = { rework_resume_seconds = 7 }")


class TrialRuns(test_resume.Resume):
    def setUp(self):
        super().setUp()
        self.ways = Path(self.tmp.name) / "ways"
        self.ways.mkdir()
        self.saved = dispatch.TRIALS_FILE
        dispatch.TRIALS_FILE = self.ways / "trials.toml"

    def tearDown(self):
        dispatch.TRIALS_FILE = self.saved
        super().tearDown()

    def number_in(self, arm, body=PROMPT):
        declare(self.ways, body)
        (trial,) = trials.load(self.ways / "trials.toml", dispatch.PROMPTS, dispatch.policy_mod.BOUNDS)
        return next(n for n in range(1, 500) if trials.arm(trial, f"t#{n}") == arm)

    def go(self, number, cfg_extra=None, kind="review"):
        board = FakeBoard([self.issue])
        board.pr_for_branch = lambda branch: None
        cfg = cfg_with(self.root)
        cfg.update(cfg_extra or {})
        d = dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda m: self.logs.append(m))
        d.prepare_worktree = lambda job, provider=None: (self.wt(job.role), "i7-x")
        d.remote_head = lambda branch: "feed000"
        d.prompt = lambda job, branch: d.ways["review"] + f"|{d.policy('rework_resume_seconds')}"
        d.assess = lambda job, branch, before, started, result: ("done", [], "ok")
        d.preserve_work = lambda job, worktree, branch: None
        d.pushed_commits = lambda branch: 0
        d.cleanup_worktree = lambda path, run_id=None: None
        seen = {}
        orig_git, orig_run = dispatch.git, dispatch.run_worker

        def fake_run(prov, role, prompt, *a, **k):
            seen["prompt"] = prompt
            return workers.WorkerResult(ok=True, final_text="x", session_id="new")
        dispatch.git, dispatch.run_worker = lambda args, cwd: "", fake_run
        try:
            pr = {"number": 9, "headRefName": f"i{number}-x", "headRefOid": "abc"}
            d.run_job(dispatch.Job("review", "reviewer", pr=pr), "claude", "sonnet")
        finally:
            dispatch.git, dispatch.run_worker = orig_git, orig_run
        self.after = d
        return seen["prompt"], [o["kind"] + ":" + o["ref"] for o in self.ledger.recent_runs(limit=1)[0]["outputs"]]

    logs = []

    def test_prompt_variant_only_in_its_arm_and_both_arms_record(self):
        self.logs = []
        text, outs = self.go(self.number_in("variant"))
        self.assertTrue(text.startswith("VARIANT REVIEW"))
        self.assertEqual(outs, ["trial:t1=variant"])
        text, outs = self.go(self.number_in("control"))
        self.assertEqual(text.split("|")[0], dispatch.WAYS["review"])
        self.assertEqual(outs, ["trial:t1=control"])
        self.assertIs(self.after.ways, dispatch.WAYS)   # nothing lingers after the run

    def test_policy_variant_changes_the_number_only_in_its_arm(self):
        text, outs = self.go(self.number_in("variant", POLICY))
        self.assertEqual(text.split("|")[1][:1], "7")
        text, outs = self.go(self.number_in("control", POLICY))
        self.assertTrue(text.split("|")[1].startswith(str(dispatch.POLICY["rework_resume_seconds"])))
        self.assertEqual(outs, ["trial:t1=control"])

    def test_a_project_ways_override_wins_and_records_nothing(self):
        n = self.number_in("variant")
        folder = self.root / "team" / "prompts"
        folder.mkdir(parents=True)
        (folder / "review.md").write_text("PROJECT REVIEW", encoding="utf-8")
        text, outs = self.go(n, {"ways": {"dir": "team"}})
        self.assertTrue(text.startswith("PROJECT REVIEW"))
        self.assertEqual(outs, [])

    def test_a_malformed_file_logs_and_the_job_runs_with_no_trial(self):
        self.logs = []
        (self.ways / "trials.toml").write_text("[[trial]]\nname = 'x'\n", encoding="utf-8")
        text, outs = self.go(7)
        self.assertEqual(text.split("|")[0], dispatch.WAYS["review"])
        self.assertEqual(outs, [])
        self.assertTrue(any("trials not applied" in m for m in self.logs))

    def test_a_file_with_no_trial_records_nothing(self):
        (self.ways / "trials.toml").write_text("# none", encoding="utf-8")
        self.assertEqual(self.go(7)[1], [])

    def test_the_shipped_file_declares_rework_fresh(self):
        from datetime import date, timedelta
        found = trials.load(trials.FILE, dispatch.PROMPTS, {n: dispatch.policy_mod.BOUNDS[n] for n in dispatch.TRIAL_POLICY})
        self.assertEqual([t.name for t in found], ["rework-fresh"])
        (t,) = found
        self.assertEqual(t.policy, {"rework_resume_seconds": 0})
        self.assertEqual(t.prompts, {})
        self.assertEqual(trials.active(found, date(2026, 11, 24)), [t])
        self.assertEqual(trials.active(found, date(2026, 11, 23)), [])
        self.assertEqual(t.max_days, 21)
        self.assertEqual(t.measure, "review_runs_per_landing")
        self.assertEqual(t.share, 0.3)
        self.assertEqual(t.min_landings, 10)

    def test_shipped_trial_arms_read_rework_resume_0_and_3300(self):
        from datetime import date
        from unittest import mock
        (t,) = trials.load(trials.FILE, dispatch.PROMPTS, {n: dispatch.policy_mod.BOUNDS[n] for n in dispatch.TRIAL_POLICY})
        dispatch.TRIALS_FILE = trials.FILE
        class Day(date):
            @classmethod
            def today(cls):
                return date(2026, 11, 24)
        for want, arm in ((0, "variant"), (3300, "control")):
            d = dispatch.Dispatcher(cfg_with(self.root), self.ledger, board=FakeBoard([]), log=lambda *_: None)
            n = next(n for n in range(1, 500) if trials.arm(t, f"{d.project}#{n}") == arm)
            with mock.patch.object(dispatch.datetime, "date", Day):
                d.enter_trials(dispatch.Job("review", "reviewer", pr={"number": 9, "headRefName": f"i{n}-x"}))
            self.assertEqual(d.policy("rework_resume_seconds"), want)
            self.assertEqual(d.trial_refs, [f"rework-fresh={arm}"])

    def test_a_malformed_file_logs_and_the_job_runs_with_no_trial(self):
        self.logs = []
        (self.ways / "trials.toml").write_text("[[trial]]\nname = 'x'\n", encoding="utf-8")
        text, outs = self.go(7)
        self.assertEqual(text.split("|")[0], dispatch.WAYS["review"])
        self.assertEqual(outs, [])
        self.assertTrue(any("trials not applied" in m for m in self.logs))

    def test_a_file_with_no_trial_records_nothing(self):
        (self.ways / "trials.toml").write_text("# none", encoding="utf-8")
        self.assertEqual(self.go(7)[1], [])

    def test_the_shipped_file_declares_rework_fresh(self):
        from datetime import date, timedelta
        found = trials.load(trials.FILE, dispatch.PROMPTS, {n: dispatch.policy_mod.BOUNDS[n] for n in dispatch.TRIAL_POLICY})
        self.assertEqual([t.name for t in found], ["rework-fresh"])
        (t,) = found
        self.assertEqual(t.policy, {"rework_resume_seconds": 0})
        self.assertEqual(t.prompts, {})
        self.assertEqual(trials.active(found, date(2026, 11, 24)), [t])
        self.assertEqual(trials.active(found, date(2026, 11, 23)), [])
        self.assertEqual(t.max_days, 21)
        self.assertEqual(t.measure, "review_runs_per_landing")
        self.assertEqual(t.share, 0.3)
        self.assertEqual(t.min_landings, 10)

    def test_shipped_trial_arms_read_rework_resume_0_and_3300(self):
        (t,) = trials.load(trials.FILE, dispatch.PROMPTS, {n: dispatch.policy_mod.BOUNDS[n] for n in dispatch.TRIAL_POLICY})
        dispatch.TRIALS_FILE = trials.FILE
        today = dispatch.date.today() if hasattr(dispatch, "date") else None
        for want, arm in ((0, "variant"), (3300, "control")):
            n = next(n for n in range(1, 500) if trials.arm(t, f"t#{n}") == arm)
            d = dispatch.Dispatcher(cfg_with(self.root), self.ledger, board=FakeBoard([]), log=lambda *_: None)
            d.trial_policy = {"rework_resume_seconds": 0} if arm == "variant" else {}
            self.assertEqual(d.policy("rework_resume_seconds"), want)

    def test_a_job_with_no_issue_is_never_in_a_trial(self):
        self.number_in("variant")
        d = dispatch.Dispatcher(cfg_with(self.root), self.ledger, board=FakeBoard([]), log=lambda *_: None)
        d.enter_trials(dispatch.Job("direct", "director", reason="x"))
        self.assertEqual(d.trial_refs, [])
        self.assertIs(d.ways, dispatch.WAYS)

    def test_a_policy_number_read_outside_a_job_is_not_a_trial_number(self):
        self.logs = []
        body = POLICY.replace("rework_resume_seconds = 7", "review_rounds = 3")
        text, outs = self.go(self.number_in("variant", body))
        self.assertEqual(outs, [])
        self.assertTrue(any("trials not applied" in m and "review_rounds" in m for m in self.logs), self.logs)


class TrialOutputIsNotProduce(unittest.TestCase):
    def test_failed_runs_under_a_trial_still_trip_no_output(self):
        from commonroom import gate
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "room.sqlite3", synchronous="OFF")
            try:
                for i in range(3):
                    run = ledger.start_run("game", f"#{i}", "builder", "codex", now=100 + i)
                    ledger.finish_run(run, "failed", outputs=[{"kind": "trial", "ref": "t1=variant"}], now=101 + i)
                runs = ledger.recent_runs("game", limit=3, autonomous=True, finished=True)
                verdict, trip = gate.breaker_verdict([], runs, 3)
                self.assertFalse(verdict.allowed)
                self.assertEqual(trip["kind"], "no_output")
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
