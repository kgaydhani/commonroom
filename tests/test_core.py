import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from commonroom import config, gate, usage
from commonroom.ledger import Ledger

NOW = 1_791_200_000.0  # fixed clock for deterministic tests
CLAUDE = config.DEFAULTS["budget"]["claude"]


def readings(five_used, week_used, five_elapsed=0.5, week_elapsed=0.5, age=10):
    return {
        "five_hour": {"used_pct": five_used, "resets_at": NOW + (1 - five_elapsed) * 5 * 3600, "observed_at": NOW - age},
        "seven_day": {"used_pct": week_used, "resets_at": NOW + (1 - week_elapsed) * 7 * 86400, "observed_at": NOW - age},
    }


class UsageParsing(unittest.TestCase):
    def test_statusline_rate_limits(self):
        data = {"rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                                "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}
        obs = {o["window"]: o for o in usage.from_statusline(data)}
        self.assertEqual(obs["five_hour"]["used_pct"], 23.5)
        self.assertEqual(obs["seven_day"]["resets_at"], 1738857600)
        self.assertEqual(usage.from_statusline({}), [])

    def test_stream_event_unified_windows(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.12, "resetsAt": 1791210000},
            "seven_day": {"utilization": 0.3, "resetsAt": 1791500000}}}}
        obs = {o["window"]: o for o in usage.from_claude_event(event)}
        self.assertAlmostEqual(obs["five_hour"]["used_pct"], 12.0)
        self.assertEqual(usage.from_claude_event({"type": "assistant"}), [])

    def test_codex_buckets(self):
        data = {"rateLimits": {"primary": {"usedPercent": 40, "resetsAt": 1791210000, "windowDurationMins": 300},
                               "secondary": {"usedPercent": 70, "resetsAt": 1791500000, "windowDurationMins": 10080}}}
        obs = {o["window"]: o["used_pct"] for o in usage.normalize_codex(data)}
        self.assertEqual(obs, {"five_hour": 40, "seven_day": 70})

    def test_claude_usage_screen(self):
        tz = "Europe/London"
        now = datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo(tz)).timestamp()
        screen = f"""
        Current session
        ███ 9% used
        Resets 1pm ({tz})
        Current week (all models)
        █ 6% used
        Resets Oct 8, 2am ({tz})
        Extra usage not enabled
        """
        obs = {o["window"]: o for o in usage.parse_claude_screen(screen, now)}
        self.assertEqual(obs["five_hour"]["used_pct"], 9)
        self.assertEqual(datetime.fromtimestamp(obs["five_hour"]["resets_at"], ZoneInfo(tz)).hour, 13)
        self.assertEqual(datetime.fromtimestamp(obs["seven_day"]["resets_at"], ZoneInfo(tz)).day, 8)

    def test_usage_limit_messages(self):
        codex = ('{"type": "error", "message": "You\u2019ve hit your usage limit. Upgrade to Pro, visit '
                 'https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 8:31 PM."}')
        when = datetime.fromtimestamp(usage.limit_hit([codex], NOW)).astimezone()
        self.assertEqual((when.hour, when.minute), (20, 31))
        self.assertGreater(usage.limit_hit([codex], NOW), NOW)
        self.assertEqual(usage.limit_hit(["Claude AI usage limit reached|1759700000"], NOW), 1759700000.0)
        tz = "Europe/London"
        when = datetime.fromtimestamp(usage.limit_hit(["You've hit your limit · resets 10pm (Europe/London)"], NOW), ZoneInfo(tz))
        self.assertEqual(when.hour, 22)
        self.assertEqual(usage.limit_hit(["usage limit reached, no time given"], NOW), NOW + 1800)
        self.assertIsNone(usage.limit_hit(["tests failed: 3", "rate of fire limit is 4"], NOW))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            usage.observation("claude", "five_hour", float("nan"), None, "x")


class Gate(unittest.TestCase):
    def verdict(self, r, policy=CLAUDE):
        return gate.usage_verdict("claude", r, policy, NOW, 600)

    def test_allows_when_on_pace(self):
        self.assertTrue(self.verdict(readings(20, 30)).allowed)

    def test_unknown_is_not_free(self):
        r = readings(20, 30); del r["seven_day"]
        self.assertFalse(self.verdict(r).allowed)

    def test_stale_reading_far_from_limits_is_allowed(self):
        v = self.verdict(readings(12, 2, age=13 * 60))
        self.assertTrue(v.allowed); self.assertIn("far from limits", v.reason)

    def test_stale_reading_near_limits_holds(self):
        v = self.verdict(readings(70, 30, five_elapsed=0.99, age=13 * 60))
        self.assertFalse(v.allowed); self.assertIn("too close", v.reason)

    def test_very_old_reading_holds(self):
        self.assertIn("old", self.verdict(readings(20, 30, age=7200)).reason)

    def test_reset_since_reading_holds(self):
        r = readings(20, 30); r["five_hour"]["resets_at"] = NOW - 1
        self.assertIn("reset", self.verdict(r).reason)

    def test_weekly_reserve(self):
        v = self.verdict(readings(20, 76, week_elapsed=0.99))
        self.assertFalse(v.allowed); self.assertIn("weekly reserve", v.reason)

    def test_five_hour_reserve(self):
        v = self.verdict(readings(91, 30, five_elapsed=0.99))
        self.assertFalse(v.allowed); self.assertIn("5-hour reserve", v.reason)

    def test_cannot_burn_fresh_window_in_minutes(self):
        # 10 minutes into a window: 30% front-load + ~3% elapsed of the 90% budget.
        v = self.verdict(readings(45, 10, five_elapsed=10 / 300))
        self.assertFalse(v.allowed); self.assertIn("burn cap", v.reason)

    def test_cannot_spend_week_overnight(self):
        # One day into the week, 40% used: pace allows 75 * 1/7 + 8 = ~18.7%.
        v = self.verdict(readings(10, 40, week_elapsed=1 / 7))
        self.assertFalse(v.allowed); self.assertIn("weekly pace", v.reason)

    def test_codex_can_run_dry(self):
        v = gate.usage_verdict("codex", readings(90, 94, week_elapsed=0.1), config.DEFAULTS["budget"]["codex"], NOW, 600)
        self.assertTrue(v.allowed)
        # ...but not on the last few points: at 96% Codex refused the job outright (2026-10-05).
        v = gate.usage_verdict("codex", readings(96, 32), config.DEFAULTS["budget"]["codex"], NOW, 600)
        self.assertFalse(v.allowed); self.assertIn("nearly empty", v.reason)


class LedgerAndBreakers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def test_unchanged_readings_refresh_instead_of_duplicating(self):
        o = usage.observation("claude", "five_hour", 10, NOW + 100, "statusline")
        self.assertEqual(self.ledger.record_usage([o], now=NOW), 1)
        self.assertEqual(self.ledger.record_usage([o], now=NOW + 50), 0)
        self.assertEqual(self.ledger.latest_usage("claude")["five_hour"]["observed_at"], NOW + 50)
        self.ledger.record_usage([usage.observation("claude", "five_hour", 12, NOW + 100, "stream")], now=NOW + 60)
        self.assertEqual(len(self.ledger.usage_history("claude", "five_hour", 0)), 2)

    def test_no_output_breaker_trips_and_resets(self):
        for i in range(3):
            run = self.ledger.start_run("game", f"#{i}", "builder", "codex", now=NOW + i)
            self.ledger.finish_run(run, "failed", now=NOW + i + 1)
        runs = self.ledger.recent_runs("game", limit=3, autonomous=True, finished=True)
        verdict, trip = gate.breaker_verdict(self.ledger.open_breakers("game"), runs, 3)
        self.assertFalse(verdict.allowed); self.assertEqual(trip["kind"], "no_output")
        self.ledger.trip("game", trip["kind"], trip["detail"])
        self.assertFalse(gate.breaker_verdict(self.ledger.open_breakers("game"), [], 3)[0].allowed)
        self.assertEqual(self.ledger.clear_breakers("game"), 1)
        self.assertTrue(gate.breaker_verdict(self.ledger.open_breakers("game"), [], 3)[0].allowed)

    def test_output_keeps_breaker_closed(self):
        for i in range(3):
            run = self.ledger.start_run("game", f"#{i}", "builder", "claude", now=NOW + i)
            self.ledger.finish_run(run, "done", outputs=[{"kind": "pr", "ref": f"#{10 + i}"}] if i == 1 else [], now=NOW + i + 1)
        runs = self.ledger.recent_runs("game", limit=3, autonomous=True, finished=True)
        self.assertTrue(gate.breaker_verdict([], runs, 3)[0].allowed)


class Config(unittest.TestCase):
    def test_room_toml_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "room.toml"
            path.write_text('[project]\nname = "x"\n[budget.claude]\nreserve_weekly = 20\n', encoding="utf8")
            cfg = config.load(path)
            self.assertEqual(cfg["budget"]["claude"]["reserve_weekly"], 20)
            self.assertEqual(cfg["budget"]["claude"]["reserve_5h"], 10)
            self.assertEqual(config.find_config(Path(tmp)), path)


if __name__ == "__main__":
    unittest.main()
