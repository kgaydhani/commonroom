"""Per-merge metrics from ledger rows (commonroom#211)."""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from commonroom import cli, config, dashboard, metrics, workers
from commonroom.ledger import Ledger

from test_dispatch import cfg_with

NOW = 1_800_000_000.0
MIN = 60


class Fixture:
    """A ledger with two merged PRs in the last week and one ten days back."""

    def __init__(self, ledger: Ledger, root: Path):
        self.ledger, self.root, self.count = ledger, root, 0

    def run(self, role, provider, model, task, ago, minutes, outputs=(), tokens=None, read=None, write=None,
            status="done", note=None):
        start = NOW - ago * MIN
        rid = self.ledger.start_run("t", task, role, provider, model, now=start)
        if read is not None or write is not None:
            self.count += 1
            wt = self.root / f"wt{self.count}"
            self.ledger.set_worktree(rid, wt)
            self.assertTrue(self.ledger.record_cache_tokens(wt, read, write, now=start + 1))
        if status != "running":
            self.ledger.finish_run(rid, status, list(outputs), tokens, 10, note=note, now=start + minutes * MIN)
        return rid

    def assertTrue(self, value):
        assert value

    def build(self):
        pr10, pr11 = {"kind": "pr", "ref": "PR #10"}, {"kind": "pr", "ref": "PR #11"}
        review = lambda n, v: [{"kind": "review", "ref": f"PR #{n} {v}"}]
        # PR #10: built by claude:opus, two CHANGES rounds, the second fix a resumed session.
        self.run("builder", "claude", "opus", "#1", 600, 30, [pr10], 1_000_000, 600_000, 300_000)
        self.run("reviewer", "codex", "gpt-5", "PR #10", 560, 10, review(10, "CHANGES"), 200_000)
        self.run("builder", "claude", "opus", "#1", 540, 20, [pr10], 500_000, 450_000, 20_000,
                 note="resumed session s1 from run r1; ok")
        self.run("reviewer", "codex", "gpt-5", "PR #10", 510, 10, review(10, "CHANGES"), 200_000)
        self.run("builder", "claude", "opus", "#1", 490, 10, [pr10], 300_000, 250_000, 10_000)
        self.run("reviewer", "codex", "gpt-5", "PR #10", 470, 10, review(10, "APPROVE"), 200_000)
        self.run("merge", "none", None, "#1", 450, 0, [{"kind": "merge", "ref": "PR #10"}])
        # PR #11: built by codex:gpt-5, approved first time.
        self.run("builder", "codex", "gpt-5", "#2", 400, 30, [pr11], 400_000)
        self.run("reviewer", "claude", "opus", "PR #11", 360, 10, review(11, "APPROVE"), 300_000, 200_000, 50_000)
        self.run("merge", "none", None, "#2", 340, 0, [{"kind": "merge", "ref": "PR #11"}])
        # A director run counts; a room-error, a running run and an update row don't.
        self.run("director", "claude", "opus", None, 300, 20, [{"kind": "director", "ref": "x"}], 900_000, 800_000, 50_000)
        self.run("builder", "claude", "opus", "#3", 200, 1, status="room-error")
        self.run("builder", "claude", "opus", "#4", 10, 0, status="running")
        self.run("update", "none", None, "#2", 5, 0, [{"kind": "update", "ref": "PR #11"}])
        # Ten days back, before the split was recorded: only in the 30-day window, and its split is n/a.
        self.run("builder", "claude", "opus", "#5", 14400, 60, [{"kind": "pr", "ref": "PR #12"}], 2_000_000)
        self.run("merge", "none", None, "#5", 14280, 0, [{"kind": "merge", "ref": "PR #12"}])


class Metrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3", synchronous="OFF")
        Fixture(self.ledger, self.root).build()
        self.rows = self.ledger.runs_since("t", NOW - metrics.LOOKBACK)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_per_merge_arithmetic_over_seven_days(self):
        w = metrics.report(self.rows, NOW, config.DEFAULTS["targets"])["7d"]
        self.assertEqual((w["merges"], w["runs"]), (2, 9))       # the resumed run is a run; room-error/running/update aren't
        self.assertEqual(w["input_tokens_per_merge"], 2_000_000)
        self.assertEqual((w["uncached_per_merge"], w["cache_write_per_merge"], w["cache_read_per_merge"]),
                         (135_000, 215_000, 1_150_000))
        self.assertEqual(w["runs_with_cache_split"], 5)
        self.assertEqual((w["review_runs_per_merge"], w["changes_rounds_per_merge"]), (2.0, 1.0))
        self.assertEqual(w["runs_per_merge"], 4.5)
        self.assertEqual(w["runs_per_merge_by_role"], {"builder": 2.0, "director": 0.5, "reviewer": 2.0})
        self.assertEqual(w["minutes_per_merge"], 75.0)
        self.assertEqual(w["by_builder_model"]["claude:opus"],
                         {"landings": 1, "review_runs": 3, "changes_rounds": 2,
                          "review_runs_per_merge": 3.0, "changes_rounds_per_merge": 2.0})
        self.assertEqual(w["by_builder_model"]["codex:gpt-5"]["review_runs_per_merge"], 1.0)
        self.assertEqual(w["by_builder_model"]["codex:gpt-5"]["changes_rounds_per_merge"], 0.0)

    def test_thirty_days_include_the_older_merge(self):
        w = metrics.report(self.rows, NOW, {})["30d"]
        self.assertEqual((w["merges"], w["runs"], w["input_tokens_per_merge"]), (3, 10, 2_000_000))
        self.assertEqual((w["review_runs_per_merge"], w["runs_per_merge"], w["minutes_per_merge"]), (1.33, 3.33, 70.0))
        self.assertEqual(w["by_builder_model"]["claude:opus"]["review_runs_per_merge"], 1.5)

    def test_split_is_na_where_no_run_recorded_it(self):
        w = metrics.window(self.rows, NOW - 11 * metrics.DAY, NOW - 9 * metrics.DAY)
        self.assertEqual((w["merges"], w["input_tokens_per_merge"]), (1, 2_000_000))
        self.assertEqual((w["uncached_per_merge"], w["cache_write_per_merge"], w["cache_read_per_merge"]), (None, None, None))

    def test_no_merges_gives_na_not_a_division_error(self):
        w = metrics.window(self.rows, NOW - 200 * MIN, NOW)
        self.assertEqual(w["merges"], 0)
        self.assertIsNone(w["input_tokens_per_merge"])
        self.assertEqual(metrics.over_target(w, config.DEFAULTS["targets"]), [])

    def test_over_target_is_flagged_per_window(self):
        report = metrics.report(self.rows, NOW, {"review_runs_per_merge": 1.5, "input_tokens_per_merge": 4_000_000})
        self.assertEqual(report["over_target"], ["7d: review_runs_per_merge 2 > 1.5"])
        self.assertEqual(metrics.report(self.rows, NOW, config.DEFAULTS["targets"])["over_target"], [])
        tokens = metrics.report(self.rows, NOW, {"input_tokens_per_merge": 1_000_000})["over_target"]
        self.assertEqual(tokens, ["7d: input_tokens_per_merge 2.0M > 1.0M", "30d: input_tokens_per_merge 2.0M > 1.0M"])

    def test_numbers_read_for_a_person(self):
        self.assertEqual([metrics.readable(v) for v in (26_511_400, 4_000_000, 135_000, 2, 1.33, 2.5, 0)],
                         ["26.5M", "4.0M", "135k", "2", "1.33", "2.5", "0"])

    def test_landings_are_the_merge_rows(self):
        self.assertEqual([r["task"] for r in metrics.landings(self.rows, NOW - 7 * metrics.DAY, NOW)], ["#1", "#2"])

    def test_digest_and_dashboard_carry_the_metrics(self):
        cfg = cfg_with(self.root)
        cfg["project"]["repo"] = None
        cfg["targets"] = {"review_runs_per_merge": 1}
        orig = (cli.Ledger, dashboard.Ledger)
        cli.Ledger = dashboard.Ledger = lambda: Ledger(self.ledger.path, synchronous="OFF")
        try:
            got = cli.digest(cfg, 1)["metrics"]
            state = json.loads(json.dumps(dashboard.state(cfg), default=str))["metrics"]
        finally:
            cli.Ledger, dashboard.Ledger = orig
        self.assertEqual(got["targets"], {"review_runs_per_merge": 1})
        self.assertIn("7d", got)
        self.assertIn("30d", state)
        self.assertEqual(state["targets"], {"review_runs_per_merge": 1})


class Targets(unittest.TestCase):
    def test_defaults_and_a_project_override(self):
        self.assertEqual(config.DEFAULTS["targets"], {"review_runs_per_merge": 2, "input_tokens_per_merge": 4000000})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "room.toml"
            path.write_text('[project]\nname = "x"\n[targets]\nreview_runs_per_merge = 3\n', encoding="utf8")
            cfg = config.load(path)
        self.assertEqual(cfg["targets"], {"review_runs_per_merge": 3, "input_tokens_per_merge": 4000000})


class CacheSplit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_an_old_ledger_upgrades_in_place_and_old_rows_show_na(self):
        self.ledger.close()
        old = self.root / "old.sqlite3"
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, project TEXT, task TEXT, role TEXT, provider TEXT, model TEXT, "
                   "effort TEXT, autonomous INTEGER NOT NULL DEFAULT 1, started_at REAL NOT NULL, ended_at REAL, "
                   "status TEXT NOT NULL, session_id TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, "
                   "outputs TEXT NOT NULL DEFAULT '[]', note TEXT)")
        db.execute("INSERT INTO runs (id, project, role, provider, started_at, ended_at, status, input_tokens) "
                   "VALUES ('a', 't', 'builder', 'claude', 1, 2, 'done', 5)")
        db.commit(); db.close()
        self.ledger = Ledger(old, synchronous="OFF")
        row = self.ledger.runs_since("t", 0)[0]
        self.assertEqual((row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]), (5, None, None))

    def claude_worker(self, wt, ledger=None):
        """Run a fake Claude worker in `wt` that reports a result with a cache split."""
        wt.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, workers.worker_tmp(wt), True)
        script = self.root / "claude.py"
        usage = {"input_tokens": 7, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 93, "output_tokens": 4}
        script.write_text("import json, sys\nsys.stdin.read()\nprint(json.dumps(" + repr(
            {"type": "result", "usage": usage, "result": "done", "is_error": False}) + "), flush=True)\n", encoding="utf8")
        orig = workers.claude_command
        workers.claude_command = lambda *a, **k: [sys.executable, str(script)]
        try:
            return workers.run_worker("claude", workers.Role("builder", None, [], "x"), "y", wt, ledger or self.ledger,
                                      self.root / "logs")
        finally:
            workers.claude_command = orig

    def test_a_claude_worker_records_its_split_on_the_open_run_and_finish_keeps_it(self):
        wt = self.root / "i1-x"
        rid = self.ledger.start_run("t", "#1", "builder", "claude", "opus")
        other = self.ledger.start_run("t", "#2", "builder", "claude", "opus")
        self.ledger.set_worktree(rid, wt)
        self.ledger.set_worktree(other, self.root / "elsewhere")
        result = self.claude_worker(wt)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual((result.input_tokens, result.cache_read_tokens, result.cache_write_tokens), (1000, 900, 93))
        # What dispatch.run_job does next: close the run with the total. The split stays on the row.
        self.ledger.finish_run(rid, "done", [], result.input_tokens, result.output_tokens)
        rows = {r["id"]: r for r in self.ledger.runs_since("t", 0)}
        self.assertEqual((rows[rid]["input_tokens"], rows[rid]["cache_read_tokens"], rows[rid]["cache_write_tokens"]),
                         (1000, 900, 93))
        self.assertIsNone(rows[other]["cache_read_tokens"])       # another run's row is untouched

    def test_a_failing_split_write_never_changes_the_runs_result(self):
        ledger = self.ledger

        class Locked:
            def __getattr__(self, name):
                return getattr(ledger, name)

            def record_cache_tokens(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")
        rid = self.ledger.start_run("t", "#1", "builder", "claude", "opus")
        self.ledger.set_worktree(rid, self.root / "i1-x")
        result = self.claude_worker(self.root / "i1-x", Locked())
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.errors, [])
        self.assertEqual((result.final_text, result.input_tokens, result.cache_read_tokens, result.cache_write_tokens),
                         ("done", 1000, 900, 93))

    def test_only_the_newest_running_row_on_the_path_gets_the_split(self):
        wt = self.root / "i1-x"
        orphan = self.ledger.start_run("t", "#1", "builder", "claude", "opus", now=NOW - 3000)   # a dead dispatcher's
        current = self.ledger.start_run("t", "#1", "builder", "claude", "opus", now=NOW - 60)
        for rid in (orphan, current):
            self.ledger.set_worktree(rid, wt)
        self.assertTrue(self.ledger.record_cache_tokens(wt, 900, 93, now=NOW))
        rows = {r["id"]: r for r in self.ledger.runs_since("t", 0)}
        self.assertEqual((rows[current]["cache_read_tokens"], rows[current]["cache_write_tokens"]), (900, 93))
        self.assertEqual((rows[orphan]["cache_read_tokens"], rows[orphan]["cache_write_tokens"]), (None, None))

    def test_a_codex_run_records_no_split(self):
        rid = self.ledger.start_run("t", "#1", "builder", "codex", "gpt-5")
        self.ledger.finish_run(rid, "done", [], 100, 1)
        row = self.ledger.runs_since("t", 0)[0]
        self.assertEqual((row["cache_read_tokens"], row["cache_write_tokens"]), (None, None))


if __name__ == "__main__":
    unittest.main()
