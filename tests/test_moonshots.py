import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from commonroom import cli, config, dispatch, gate, moonshots as ms, workers
from commonroom.ledger import Ledger


def card(*rows, round_number=1):
    return {"round": round_number, "rows": [dict(id=id, bar=f"bar for {id}", value=value, met=met)
                                           for id, value, met in rows]}


class Decisions(unittest.TestCase):
    def test_active_accounting_excludes_pauses_and_clock_rollback(self):
        used = ms.active_time(60, 10, 40)
        self.assertEqual(used, 90)
        self.assertEqual(ms.active_time(used, 40, 10000, running=False), 90)
        self.assertEqual(ms.active_time(used, 40, 20), 90)

    def test_guards_thresholds_and_model_specific_windows(self):
        policy = config.DEFAULTS["moonshot"]
        windows = {"five_hour": {"used_pct": 80, "resets_at": 200},
                   "seven_day_fable": {"used_pct": 85}, "seven_day": {"used_pct": 85}}
        self.assertEqual(ms.guard_decision(windows, policy, "fable", 100), ("pause", "5-hour window hot", 320))
        windows["seven_day_fable"]["used_pct"] = 86
        self.assertEqual(ms.guard_decision(windows, policy, "fable", 100)[0], "stop")
        self.assertEqual(ms.guard_decision(windows, policy, "opus", 100)[0], "pause")
        windows["seven_day"]["used_pct"] = 86
        self.assertIn("seven_day above", ms.guard_decision(windows, policy, "opus", 100)[1])

    def test_guards_ignore_expired_windows_and_honor_overrides(self):
        windows = {"five_hour": {"used_pct": 99, "resets_at": 90},
                   "seven_day": {"used_pct": 99, "resets_at": 90}}
        self.assertEqual(ms.guard_decision(windows, {}, "fable", 100)[0], "go")
        windows["seven_day"]["resets_at"] = 200
        self.assertEqual(ms.guard_decision(windows, {"stop_weekly": 100}, "fable", 100)[0], "go")

    def test_gauntlet_names_all_misses_and_regressions(self):
        before = card(("a", "100%", True), ("b", "2/5", False))
        after = card(("a", "40%", False), ("b", "3/5", False), ("c", "ok", True))
        prompt = ms.next_prompt(after, before, 2, 5, 3600, "report.md")
        for text in ("a: bar=bar for a; value=40%", "b: bar=bar for b; value=3/5", "REGRESSED", "round 3"):
            self.assertIn(text, prompt)
        self.assertNotIn("c: bar", prompt)

    def test_gauntlet_stopping_boundaries(self):
        failed = card(("a", "no", False))
        self.assertIsNone(ms.next_prompt(card(("a", "yes", True)), None, 1, 5, 9000, "r.md"))
        self.assertIsNone(ms.next_prompt(failed, None, 5, 5, 9000, "r.md"))
        self.assertIsNone(ms.next_prompt(failed, None, 1, 5, 1500, "r.md"))
        self.assertIsNotNone(ms.next_prompt(failed, None, 1, 5, 1501, "r.md"))
        self.assertIn("missing or unparseable", ms.next_prompt(None, None, 1, 5, 9000, "r.md"))

    def test_scorecard_validation_rejects_ambiguous_success(self):
        valid = card(("a", "94%", True))
        self.assertEqual(ms.parse_scorecard(json.dumps(valid)), valid)
        invalid = ["oops", "null", "[]", "{}", '{"round":1,"rows":[]}',
                   json.dumps(card(("a", "94%", "false"))),
                   json.dumps(card(("a", "94%", True), ("a", "0%", False)))]
        for text in invalid:
            self.assertIsNone(ms.parse_scorecard(text), text)

    def test_verifier_overrides_rows_and_disappeared_rows_regress(self):
        before = card(("old", "ok", True))
        own = card(("a", "100%", True), ("b", "ok", True))
        verified = card(("a", "70%", False), ("new", "yes", True))
        result = ms.merge_scorecards(own, verified, before)
        rows = {r["id"]: r for r in result["rows"]}
        self.assertEqual(rows["a"]["value"], "70%")
        self.assertFalse(rows["old"]["met"])
        self.assertIn("REGRESSED", ms.next_prompt(result, before, 1, 5, 9000, "r.md"))

    def test_recovery_decisions(self):
        for status in ("running", "paused"):
            job = dict(status=status, start_after=100)
            self.assertEqual(ms.recovery_decision(job, False, 90), "recover")
            self.assertIsNone(ms.recovery_decision(job, True, 100))
        self.assertEqual(ms.recovery_decision(dict(status="queued", start_after=100), False, 100), "launch")
        self.assertIsNone(ms.recovery_decision(dict(status="queued", start_after=100), False, 99))
        for status in ms.TERMINAL:
            self.assertIsNone(ms.recovery_decision(dict(status=status, start_after=0), False, 100))

    def test_local_start_rolls_forward_and_epoch_is_exact(self):
        now = datetime(2026, 10, 7, 12, 0).timestamp()
        self.assertEqual(ms.parse_time("13:00", now), datetime(2026, 10, 7, 13, 0).timestamp())
        self.assertEqual(ms.parse_time("11:00", now), datetime(2026, 10, 8, 11, 0).timestamp())
        self.assertEqual(ms.parse_time("123456", now), 123456)
        for invalid in ("nan", "inf", "-1", "25:30"):
            with self.assertRaises(ValueError):
                ms.parse_time(invalid, now)


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "state" / "room.sqlite3")
        self.cfg = config.merge(config.DEFAULTS, {"project": {"name": "test", "repo": "o/test"},
                                                 "min_free_gb": {"room": 0, "project": 0}})
        self.cfg["_root"] = str(self.root)
        self.brief = self.root / "source.md"
        self.brief.write_text("Build something ambitious.", encoding="utf-8")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def add(self, name="demo", **options):
        return ms.add(self.cfg, self.ledger, name, self.brief, **options)

    def job(self):
        return self.ledger.moonshot("test", "demo")


class WithLedger(LedgerFixture):
    def test_add_is_durable_and_defaults_and_custom_rules(self):
        job = self.add()
        text = Path(job["brief_path"]).read_text(encoding="utf-8")
        self.brief.unlink()
        self.assertIn("Build something ambitious", text)
        self.assertIn('"met": true', text)
        self.assertIn("Never push to main", text)
        self.assertEqual((job["budget_minutes"], job["model"], job["effort"], job["max_rounds"]), (180, "fable", "high", 5))
        self.assertEqual(Path(job["worktree"]).name, "ms-demo")
        self.assertEqual(ms.scorecard_path(job["report"]), job["report"][:-3] + ".scorecard.json")
        self.assertEqual(job["status"], "queued")
        self.brief.write_text("Other brief", encoding="utf-8")
        rules = self.root / ".claude" / "moonshot_rules.md"
        rules.parent.mkdir()
        rules.write_text("Project specific export instructions", encoding="utf-8")
        other = self.add("other", minutes=45, model="opus", effort="medium", base="release", max_rounds=2)
        self.assertIn("Project specific", Path(other["brief_path"]).read_text(encoding="utf-8"))
        self.assertEqual((other["budget_minutes"], other["base"]), (45, "release"))

    def test_validation_and_duplicate_do_not_replace_brief(self):
        original = self.add()
        self.brief.write_text("replacement", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.add()
        self.assertNotIn("replacement", Path(original["brief_path"]).read_text(encoding="utf-8"))
        for name, options in (("../bad", {}), ("no-budget", {"minutes": 0}), ("bad-round", {"max_rounds": 0}),
                              ("bad-report", {"report": "../r.md"})):
            with self.assertRaises(ValueError):
                self.add(name, **options)

    def test_feedback_is_verbatim_atomic_and_consumed_once(self):
        self.add()
        text = "love X\nhate Y  \n"
        self.ledger.moonshot_note("test", "demo", text)
        self.ledger.moonshot_note("test", "demo", "second")
        note = self.ledger.prepare_moonshot_prompt("test", "demo", "continue", "session")
        self.assertIn(text, note)
        self.assertEqual(note.count("Owner/director feedback (verbatim):"), 2)
        self.assertEqual(self.job()["next_prompt"], note)
        self.assertIsNone(self.job()["pending_note"])
        self.assertEqual(self.ledger.prepare_moonshot_prompt("test", "demo", "next", "session"), "next")

    def test_resume_retains_session_active_time_and_branch(self):
        original = self.add(minutes=30)
        self.ledger.update_moonshot("test", "demo", status="done", active_seconds_used=1800,
                                   session_id="same", round=5, until=1, issue_url="old")
        with self.assertRaises(ValueError):
            ms.resume(self.ledger, "test", "demo")
        ms.resume(self.ledger, "test", "demo", 60, "owner continuation")
        job = self.job()
        self.assertEqual((job["status"], job["session_id"], job["branch"], job["active_seconds_used"]),
                         ("queued", "same", original["branch"], 1800))
        self.assertEqual((job["budget_minutes"], job["next_prompt"], job["max_rounds"]), (90, "owner continuation", 10))
        self.assertIsNone(job["until"])
        self.assertIsNone(job["issue_url"])
        with self.assertRaises(ValueError):
            ms.resume(self.ledger, "test", "demo", 10)

    def test_stop_requests_interrupt_instead_of_marking_running_done(self):
        self.add()
        self.ledger.update_moonshot("test", "demo", status="running", runner_pid=os.getpid())
        ms.stop(self.ledger, "test", "demo")
        self.assertEqual((self.job()["status"], self.job()["stop_requested"]), ("running", 1))
        self.ledger.update_moonshot("test", "demo", status="queued", runner_pid=None)
        ms.stop(self.ledger, "test", "demo")
        self.assertEqual(self.job()["status"], "stopped")

    def test_schedule_gate_start_capacity_and_duplicate_launches(self):
        for n in ("demo", "two", "three", "four"):
            self.add(n)
        self.ledger.update_moonshot("test", "four", start_after=1000)
        spawned = []
        def spawn(cfg, ledger, job):
            spawned.append(job["name"])
            return 100 + len(spawned)
        self.assertEqual(ms.schedule(self.cfg, self.ledger, allowed=False, now=100, spawn=spawn), [])
        actions = ms.schedule(self.cfg, self.ledger, allowed=True, now=100, alive=lambda pid: True, spawn=spawn)
        self.assertEqual(len(actions), 3)
        self.assertEqual(ms.schedule(self.cfg, self.ledger, allowed=True, now=2000, alive=lambda pid: True, spawn=spawn), [])
        self.assertEqual(spawned, ["demo", "two", "three"])

    def test_launch_is_detached_and_uses_job_log_and_ledger(self):
        job = self.add()
        with patch.object(ms.subprocess, "Popen") as popen:
            popen.return_value.pid = 123
            self.assertEqual(ms.launch(self.cfg, self.ledger, job), 123)
            options = popen.call_args.kwargs
            self.assertEqual(Path(options["stdout"].name), ms.log_path(self.ledger, "test", "demo"))
            self.assertEqual(options["cwd"], self.cfg["_root"])
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            command = popen.call_args.args[0]
            self.assertEqual(command[2:5], ["moonshot", "run", "demo"])
            self.assertIn(str(self.ledger.path.resolve()), command)
            self.assertTrue(options["creationflags"] if os.name == "nt" else options["start_new_session"])

    def test_notification_failure_is_retried_without_relaunching_finished_job(self):
        self.add()
        self.ledger.update_moonshot("test", "demo", status="done", finished_at=123)
        with patch.object(ms, "notify", side_effect=RuntimeError("offline")), patch.object(ms, "launch") as launch:
            lines = ms.schedule(self.cfg, self.ledger, allowed=False)
        self.assertIn("notification pending", lines[0])
        launch.assert_not_called()
        with patch.object(ms, "notify") as notify:
            ms.schedule(self.cfg, self.ledger, allowed=False)
        notify.assert_called_once()

    def test_director_sees_harvest_without_current_milestone(self):
        from test_dispatch import FakeBoard, issue
        self.cfg["goal"]["milestone"] = None
        board = FakeBoard([issue(24, ["needs:director"], milestone=None)])
        dispatcher = dispatch.Dispatcher(self.cfg, self.ledger, board=board)
        self.assertIn("#24 escalated", dispatcher.director_trigger())

    def test_room_status_and_dashboard_include_moonshots(self):
        from commonroom import dashboard
        self.add()
        self.cfg["project"]["repo"] = None
        verdict = {p: gate.Verdict(True, "ok") for p in ("claude", "codex")}
        with patch.object(cli.config_mod, "load", return_value=self.cfg), \
                patch.object(cli, "Ledger", side_effect=lambda: Ledger(self.ledger.path)), \
                patch.object(cli, "verdicts", return_value=verdict), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status"]), 0)
        self.assertIn("Moonshots:", output.getvalue())
        self.assertIn("demo queued", output.getvalue())
        with patch.object(dashboard, "Ledger", side_effect=lambda: Ledger(self.ledger.path)), \
                patch.object(dashboard, "verdicts", return_value=verdict):
            state = dashboard.state(self.cfg)
        self.assertEqual(state["moonshots"][0]["name"], "demo")

    def test_recovery_carries_budget_session_and_inflight_feedback_when_gate_holds(self):
        self.add()
        self.ledger.update_moonshot("test", "demo", status="paused", runner_pid=999999, session_id="same",
                                   active_seconds_used=321, next_prompt="Owner/director feedback (verbatim):\nlove X\n")
        with patch.object(ms, "notify"):
            actions = ms.schedule(self.cfg, self.ledger, allowed=False, now=1000, alive=lambda pid: False,
                                  spawn=lambda *a: 123)
        job = self.job()
        self.assertEqual(len(actions), 1)
        self.assertEqual((job["session_id"], job["active_seconds_used"], job["runner_pid"]), ("same", 321, 123))
        self.assertIn("PC restarted", job["next_prompt"])
        self.assertTrue(job["next_prompt"].startswith("Owner/director feedback"))

    def test_recovery_takes_capacity_before_new_queued_jobs(self):
        self.add("waiting")
        self.add()
        self.cfg["moonshot"]["max_parallel"] = 1
        self.ledger.update_moonshot("test", "demo", status="running", runner_pid=999999, session_id="same")
        launched = []
        ms.schedule(self.cfg, self.ledger, allowed=True, alive=lambda pid: False,
                    spawn=lambda cfg, ledger, job: launched.append(job["name"]) or 123)
        self.assertEqual(launched, ["demo"])
        self.assertEqual(self.ledger.moonshot("test", "waiting")["status"], "queued")

    def test_overview_sums_segment_cost(self):
        self.add()
        for cost in (1.25, 2.5):
            run = self.ledger.start_run("test", "moonshot:demo", "moonshot", "claude")
            self.ledger.finish_run(run, "done", cost_usd=cost)
        self.ledger.update_moonshot("test", "demo", active_seconds_used=90, last_scorecard=json.dumps(card(("a", "yes", True))))
        job = ms.overview(self.ledger, "test")[0]
        self.assertEqual((job["minutes_used"], job["minutes_left"], job["cost_usd"], job["met"], job["total"]), (1.5, 178.5, 3.75, 1, 1))

    def test_migration_keeps_existing_runs(self):
        path = self.root / "old.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, project TEXT, task TEXT, role TEXT, provider TEXT, model TEXT, "
                   "effort TEXT, autonomous INTEGER, started_at REAL, ended_at REAL, status TEXT, session_id TEXT, "
                   "input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, outputs TEXT, note TEXT)")
        db.execute("INSERT INTO runs (id,status,outputs) VALUES ('old','done','[]')")
        db.commit(); db.close()
        migrated = Ledger(path)
        try:
            self.assertEqual(migrated.recent_runs()[0]["id"], "old")
            self.assertEqual(migrated.moonshots("test"), [])
        finally:
            migrated.close()

    def test_prepare_uses_base_and_reuses_existing_branch_without_reset(self):
        job = self.add(base="origin/release")
        calls = []
        def fake_git(args, cwd):
            calls.append(args)
            if args[0] == "rev-parse":
                raise RuntimeError("no branch yet")
            return job["branch"] if args[0] == "branch" else ""
        with patch.object(ms, "git", side_effect=fake_git):
            ms.prepare(self.cfg, job)
        self.assertIn(["worktree", "add", "-b", job["branch"], job["worktree"], "origin/release"], calls)
        calls.clear()
        with patch.object(ms, "git", side_effect=lambda args, cwd: calls.append(args) or job["branch"]):
            ms.prepare(self.cfg, job)
        self.assertIn(["worktree", "add", job["worktree"], job["branch"]], calls)
        self.assertFalse(any("-B" in args for args in calls))

    def test_save_uses_dispatch_retry_helper_and_checks_branch(self):
        job = self.add()
        calls = []
        def git(args, cwd):
            calls.append(args)
            return job["branch"] if args[0] == "branch" else "changed" if args[0] == "status" else ""
        with patch.object(ms, "git", side_effect=git):
            ms.save(job)
        self.assertEqual(calls[-1], ["push", "-q", "-u", "origin", job["branch"]])
        self.assertTrue(any(args[0] == "commit" for args in calls))
        with patch.object(ms, "git", return_value="main"):
            with self.assertRaises(RuntimeError):
                ms.save(job)

    def test_verifier_stdout_overrides_and_invalid_or_failed_verifier_reprompts(self):
        job = self.add(verify_cmd="check scorecard")
        path = Path(job["worktree"]) / ms.scorecard_path(job["report"])
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(card(("a", "yes", True))), encoding="utf-8")
        verified = json.dumps(card(("a", "no", False)))
        with patch.object(ms.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, verified, "")) as verify:
            result = ms.read_scorecard(job, None)
        self.assertFalse(result["rows"][0]["met"])
        self.assertEqual(verify.call_args.kwargs["cwd"], job["worktree"])
        for code, stdout in ((0, "bad"), (1, verified)):
            with patch.object(ms.subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout, "")):
                self.assertIsNone(ms.read_scorecard(job, None, log=lambda *a: None))

    def test_harvest_issue_contains_artifacts_cost_and_scorecard_and_retries_idempotently(self):
        job = self.add()
        self.ledger.update_moonshot("test", "demo", status="done", finished_at=123,
                                   last_scorecard=json.dumps(card(("a", "yes", True), ("b", "no", False))))
        with patch.object(ms.board, "gh", side_effect=[[], "https://github.com/o/test/issues/4"]) as gh:
            ms.notify(self.cfg, self.ledger, self.job())
        args = gh.call_args.args[0]
        self.assertEqual(args[:2], ["issue", "create"])
        self.assertIn("Moonshot demo finished: 1/2 bar rows met", args)
        self.assertIn("needs:director", args)
        body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        for value in (job["branch"], job["report"], "Active minutes", "Cost", "| a |", "| b |"):
            self.assertIn(value, body)
        self.ledger.update_moonshot("test", "demo", issue_url=None)
        with patch.object(ms.board, "gh", return_value=[dict(url="existing", body=body)]) as gh:
            ms.notify(self.cfg, self.ledger, self.job())
        self.assertEqual(gh.call_count, 1)
        self.assertEqual(self.job()["issue_url"], "existing")

    def test_dispatcher_does_not_close_live_old_moonshot(self):
        self.add()
        run = self.ledger.start_run("test", "moonshot:demo", "moonshot", "claude", now=1)
        dispatcher = dispatch.Dispatcher(self.cfg, self.ledger, board=object())
        self.assertEqual(dispatcher.recover_orphans(now=100000), [])
        self.assertEqual(self.ledger.running_runs("test")[0]["id"], run)

    def test_cli_commands_use_only_temp_ledger(self):
        with patch.object(cli.config_mod, "load", return_value=self.cfg), \
                patch.object(cli, "Ledger", side_effect=lambda *a: Ledger(self.ledger.path)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["moonshot", "add", "demo", "--brief", str(self.brief), "--minutes", "35"]), 0)
            self.assertEqual(cli.main(["moonshot", "list"]), 0)
            self.assertEqual(cli.main(["moonshot", "status", "demo"]), 0)
            self.assertEqual(cli.main(["moonshot", "note", "demo", str(self.brief)]), 0)
            self.assertEqual(cli.main(["moonshot", "stop", "demo"]), 0)
            self.assertEqual(cli.main(["moonshot", "resume", "demo", "--extend", "10", "--prompt-file", str(self.brief)]), 0)
        self.assertIn("35.0m left", output.getvalue())
        self.assertEqual(self.job()["budget_minutes"], 45)


class Clock:
    def __init__(self):
        self.wall = 2_000_000_000.0
        self.mono = 0.0
        self.waited = 0.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def sleep(self, seconds):
        self.waited += seconds
        self.advance(seconds)


class Runner(LedgerFixture):
    @contextlib.contextmanager
    def harness(self, worker):
        self.clock = Clock()
        tree = self.root / "tree"
        tree.mkdir(exist_ok=True)
        self.ledger.update_moonshot("test", "demo", worktree=str(tree))
        def sleep(seconds):
            job = self.job()
            wake = job["wait_until"]
            if job["until"]:
                wake = min(wake, job["until"])
            self.clock.sleep(max(seconds, wake - self.clock.wall))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ms.time, "time", side_effect=lambda: self.clock.wall))
            stack.enter_context(patch.object(ms.time, "monotonic", side_effect=lambda: self.clock.mono))
            stack.enter_context(patch.object(ms.time, "sleep", side_effect=sleep))
            stack.enter_context(patch.object(ms, "verdicts", return_value={"claude": gate.Verdict(True, "ok")}))
            stack.enter_context(patch.object(ms, "pid_alive", side_effect=lambda pid: pid == os.getpid()))
            stack.enter_context(patch.object(ms, "prepare"))
            self.saves = stack.enter_context(patch.object(ms, "save"))
            self.notify = stack.enter_context(patch.object(ms, "notify"))
            self.clean = stack.enter_context(patch.object(ms.shutil, "rmtree"))
            stack.enter_context(patch.object(ms, "run_worker", side_effect=worker))
            yield

    def write_card(self, value):
        job = self.job()
        path = Path(job["worktree"]) / ms.scorecard_path(job["report"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def result(self, options, **fields):
        return workers.WorkerResult(session_id=options["resume_session"] or options["new_session_id"], **fields)

    def test_gauntlet_uses_same_session_note_once_and_records_each_segment(self):
        self.add()
        feedback = "love X\nhate Y"
        self.ledger.moonshot_note("test", "demo", feedback)
        prompts, sessions = [], []
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            prompts.append(prompt)
            sessions.append(options["resume_session"])
            self.assertEqual(ledger.running_runs("test")[0]["session_id"], options["new_session_id"])
            self.clock.advance(60)
            self.write_card(card(("a", "yes" if len(prompts) == 2 else "no", len(prompts) == 2)))
            return self.result(options, ok=True, cost_usd=1.5)
        with self.harness(worker):
            self.assertEqual(ms.run(self.cfg, self.ledger, "demo"), 0)
        job = self.job()
        self.assertEqual((job["status"], job["round"], job["active_seconds_used"]), ("done", 2, 120))
        self.assertTrue(prompts[0].startswith("Owner/director feedback (verbatim):\n" + feedback))
        self.assertNotIn(feedback, prompts[1])
        self.assertIn("a: bar=bar for a; value=no", prompts[1])
        self.assertIsNone(sessions[0])
        self.assertEqual(sessions[1], job["session_id"])
        self.assertEqual(self.ledger.moonshot_cost("test", "demo"), 3)
        self.assertEqual(self.saves.call_count, 3)
        self.notify.assert_called_once()
        self.clean.assert_called_once()

    def test_missing_scorecard_reprompts_until_round_cap(self):
        self.add(max_rounds=2)
        prompts = []
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            prompts.append(prompt)
            self.clock.advance(10)
            return self.result(options, ok=True)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")
        self.assertEqual((self.job()["status"], self.job()["round"]), ("done", 2))
        self.assertIn("missing or unparseable", prompts[1])

    def test_pending_note_is_preserved_while_start_gate_holds(self):
        self.add()
        self.ledger.moonshot_note("test", "demo", "feedback")
        with patch.object(ms, "verdicts", return_value={"claude": gate.Verdict(False, "hold")}), \
                patch.object(ms, "run_worker") as worker:
            self.assertEqual(ms.run(self.cfg, self.ledger, "demo"), 0)
        worker.assert_not_called()
        self.assertIn("feedback", self.job()["pending_note"])

    def test_checkpoint_is_durable_before_worker_finishes(self):
        self.add()
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            self.clock.advance(7)
            self.assertIsNone(options["stop_check"]())
            other = Ledger(ledger.path)
            try:
                job = other.moonshot("test", "demo")
                self.assertEqual(job["active_seconds_used"], 7)
                self.assertEqual(job["session_id"], options["new_session_id"])
            finally:
                other.close()
            self.write_card(card(("a", "yes", True)))
            return self.result(options, ok=True)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")

    def test_provider_wait_does_not_spend_budget_or_rounds_and_delivers_next_note(self):
        self.add()
        prompts = []
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            prompts.append(prompt)
            self.clock.advance(60)
            if len(prompts) == 1:
                ledger.moonshot_note("test", "demo", "feedback during pause")
                return self.result(options, ok=False, limited_until=self.clock.wall + 3600)
            self.write_card(card(("a", "yes", True)))
            return self.result(options, ok=True)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")
        self.assertEqual(self.job()["active_seconds_used"], 120)
        self.assertEqual(self.job()["round"], 1)
        self.assertEqual(self.clock.waited, 3690)
        self.assertTrue(prompts[1].startswith("Owner/director feedback"))
        self.assertIn("continue exactly where you stopped", prompts[1].lower())
        self.assertIn("179.0 active minutes remain", prompts[1])

    def test_usage_pause_interrupts_then_resumes_after_reset(self):
        self.add()
        prompts = []
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            prompts.append(prompt)
            self.clock.advance(10)
            if len(prompts) == 1:
                ledger.record_usage([dict(provider="claude", window="five_hour", used_pct=80,
                                          resets_at=self.clock.wall + 100, source="test")])
                reason = options["stop_check"]()
                self.assertEqual(reason, "5-hour window hot")
                return self.result(options, ok=False, interrupted=reason)
            self.write_card(card(("a", "yes", True)))
            return self.result(options, ok=True)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")
        self.assertEqual(self.job()["active_seconds_used"], 20)
        self.assertEqual(self.clock.waited, 220)
        self.assertIn("paused to leave usage", prompts[1])

    def test_wrap_is_external_at_twenty_minutes_and_retains_note(self):
        self.add(minutes=30)
        prompts = []
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            prompts.append(prompt)
            if len(prompts) == 1:
                self.clock.advance(600)
                reason = options["stop_check"]()
                self.assertEqual(reason, "deliverables due")
                ledger.moonshot_note("test", "demo", "export feedback")
                return self.result(options, ok=False, interrupted=reason)
            self.assertIn(ms.WRAP, prompt)
            self.assertTrue(prompt.startswith("Owner/director feedback"))
            self.clock.advance(10)
            self.write_card(card(("a", "no", False)))
            return self.result(options, ok=True)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")
        self.assertEqual((self.job()["status"], self.job()["active_seconds_used"], self.job()["wrap_sent"]), ("done", 610, 1))
        self.assertEqual(len(prompts), 2)

    def test_budget_interrupts_wrap_and_stops_without_more_inference(self):
        self.add(minutes=10)
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            self.assertIn("Build something ambitious", prompt)
            self.assertIn(ms.WRAP, prompt)
            self.clock.advance(600)
            reason = options["stop_check"]()
            self.assertEqual(reason, "active budget used")
            return self.result(options, ok=False, interrupted=reason)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")
        self.assertEqual((self.job()["status"], self.job()["active_seconds_used"]), ("done", 600))

    def test_stop_interrupts_saves_and_notifies(self):
        self.add()
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            self.clock.advance(60)
            ms.stop(ledger, "test", "demo")
            reason = options["stop_check"]()
            self.assertEqual(reason, "owner requested stop")
            return self.result(options, ok=False, interrupted=reason)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")
        self.assertEqual((self.job()["status"], self.job()["active_seconds_used"]), ("stopped", 60))
        self.assertGreaterEqual(self.saves.call_count, 1)
        self.notify.assert_called_once()

    def test_weekly_guard_interrupts_and_stops(self):
        self.add()
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            self.clock.advance(20)
            ledger.record_usage([dict(provider="claude", window="seven_day_fable", used_pct=86,
                                      resets_at=self.clock.wall + 10000, source="test")])
            reason = options["stop_check"]()
            self.assertIn("seven_day_fable above", reason)
            return self.result(options, ok=False, interrupted=reason)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")
        self.assertEqual(self.job()["status"], "stopped")
        self.assertIn("seven_day_fable", self.job()["detail"])

    def test_restart_closes_only_abandoned_segment_and_keeps_active_minutes(self):
        self.add()
        self.ledger.update_moonshot("test", "demo", status="running", runner_pid=99999,
                                   session_id="old-session", active_seconds_used=120, checkpoint_at=12345)
        orphan = self.ledger.start_run("test", "moonshot:demo", "moonshot", "claude", session_id="old-session")
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            self.assertEqual(options["resume_session"], "old-session")
            self.assertIn("PC restarted", prompt)
            self.clock.advance(60)
            self.write_card(card(("a", "yes", True)))
            return self.result(options, ok=True)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo")
        self.assertEqual(self.job()["active_seconds_used"], 180)
        old = next(r for r in self.ledger.recent_runs("test") if r["id"] == orphan)
        self.assertEqual((old["status"], old["ended_at"]), ("stopped", 12345))

    def test_hard_deadline_can_end_a_usage_pause(self):
        self.add()
        def worker(provider, role, prompt, cwd, ledger, log_dir, **options):
            self.clock.advance(10)
            return self.result(options, ok=False, limited_until=self.clock.wall + 10000)
        with self.harness(worker):
            ms.run(self.cfg, self.ledger, "demo", until=self.clock.wall + 60)
        self.assertEqual(self.job()["active_seconds_used"], 10)
        self.assertIn("wall-clock", self.job()["detail"])

    def test_failure_preserves_checkpoint_and_closes_run(self):
        self.add()
        def worker(*args, **options):
            self.clock.advance(30)
            raise RuntimeError("worker start failure")
        with self.harness(worker):
            self.assertEqual(ms.run(self.cfg, self.ledger, "demo"), 1)
        self.assertEqual((self.job()["status"], self.job()["active_seconds_used"]), ("failed", 30))
        self.assertEqual(self.ledger.running_runs("test"), [])
        self.assertIsNone(self.job()["runner_pid"])


if __name__ == "__main__":
    unittest.main()
