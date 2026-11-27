"""commonroom#645: an off-schedule meter reset and an out-of-spread model weight become steward findings, and reach the
director through the ledger's findings like the others. Temporary ledger and home; nothing reads the live room."""
import json
import time
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from commonroom import steward, weights
from commonroom.ledger import Ledger
from tests import notrials
from tests.test_dispatch import FakeBoard, cfg_with
from commonroom import dispatch

setUpModule, tearDownModule = notrials.start, notrials.stop

DAY = 86400.0
T0 = 1_800_000_000.0


def reading(at, pct, resets):
    return {"first_seen": at, "observed_at": at, "used_pct": pct, "resets_at": resets}


def fit(date, dpp, spread=0.01):
    return {"date": date, "meter": "seven_day", "days": 8, "spread": 0.1, "reset_days": [], "left_out": [],
            "models": {"opus": {"dollars_per_point": dpp, "spread": spread}}}


class OffSchedule(unittest.TestCase):
    def test_alternating_sources_count_only_new_highs(self):
        start = T0 - T0 % DAY
        due = start + 5 * 3600
        for disagreement in (1, 2):
            with self.subTest(disagreement=disagreement):
                rs = []
                for i, pct in enumerate((31, 31, 33, 33 - disagreement, 33, 35, 35 - disagreement, 35)):
                    source = "stream" if i % 2 else "usage-screen"
                    rs.append(dict(reading(start + i * 1800, pct, due), source=source))
                self.assertEqual(steward.off_schedule_resets(rs, "five_hour", start), [])
                day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
                self.assertEqual(weights.daily_rises(rs), ({day: 4.0}, []))

    def test_ten_point_drop_two_hours_early_with_unchanged_due(self):
        due = T0 + 5 * 3600
        at = due - 2 * 3600
        rs = [reading(T0, 31, due), reading(at, 21, due), reading(at + 60, 24, due)]
        self.assert_early_reset(rs, at, expected_rise=3.0)

    def test_due_moves_early_even_though_meter_rises(self):
        due = T0 + 5 * 3600
        at = due - 2 * 3600
        rs = [reading(T0, 31, due), reading(at, 32, due + 3600)]
        self.assert_early_reset(rs, at)

    def assert_early_reset(self, readings, at, expected_rise=None):
        found = steward.off_schedule_resets(readings, "five_hour", T0)
        self.assertEqual([f["invariant"] for f in found], ["off-schedule-reset"])
        day = datetime.fromtimestamp(at, timezone.utc).date().isoformat()
        expected_rises = {} if expected_rise is None else {day: expected_rise}
        self.assertEqual(weights.daily_rises(readings), (expected_rises, [day]))

    def test_reset_two_days_early_is_a_finding_and_names_the_day(self):
        due = T0 + 4 * DAY
        rs = [reading(T0, 40.0, due), reading(T0 + 2 * DAY, 3.0, T0 + 9 * DAY)]
        found = steward.off_schedule_resets(rs, "seven_day", T0)
        self.assertEqual([f["invariant"] for f in found], ["off-schedule-reset"])
        self.assertIn("2.0 days before", found[0]["detail"])
        self.assertIn("seven_day", found[0]["detail"])

    def test_a_scheduled_reset_gives_none(self):
        due = T0 + 2 * DAY
        rs = [reading(T0, 40.0, due), reading(due + 60, 1.0, due + 7 * DAY), reading(due + DAY, 9.0, due + 7 * DAY)]
        self.assertEqual(steward.off_schedule_resets(rs, "seven_day", T0), [])

    def test_old_resets_age_out(self):
        rs = [reading(T0, 40.0, T0 + 4 * DAY), reading(T0 + 2 * DAY, 3.0, T0 + 9 * DAY)]
        self.assertEqual(steward.off_schedule_resets(rs, "seven_day", T0 + 3 * DAY), [])


class OddWeights(unittest.TestCase):
    base = [fit(f"2026-11-0{d}", v) for d, v in zip(range(1, 7), (1.0, 1.05, 0.95, 1.0, 1.02, 0.98))]

    def test_a_day_outside_the_spread_names_model_and_day(self):
        found = steward.odd_weights(self.base + [fit("2026-11-07", 2.0)], "seven_day")
        self.assertEqual(len(found), 1)
        self.assertIn("opus", found[0]["detail"])
        self.assertIn("2026-11-07", found[0]["detail"])

    def test_a_normal_day_gives_none(self):
        self.assertEqual(steward.odd_weights(self.base + [fit("2026-11-07", 1.03)], "seven_day"), [])

    def test_too_little_history_gives_none(self):
        self.assertEqual(steward.odd_weights([fit("2026-11-01", 1.0), fit("2026-11-02", 5.0)], "seven_day"), [])


class Reach(unittest.TestCase):
    def test_housekeeping_keeps_both_findings_in_the_ledger_and_changes_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "room.sqlite3")
            try:
                self.run_it(tmp, ledger)
            finally:
                ledger.close()

    def run_it(self, tmp, ledger):
        if True:
            d = dispatch.Dispatcher(cfg_with(tmp), ledger, board=FakeBoard([]), log=lambda *_: None)
            d.housekeeping()
            self.assertEqual(ledger.findings("t"), [])         # no readings, no history: a project's pass is as before
            now = time.time()
            for pct, at, resets in ((40.0, now - 2.5 * DAY, now + DAY), (3.0, now - DAY, now + 6 * DAY)):
                ledger.record_usage([{"provider": "claude", "window": "seven_day", "used_pct": pct, "resets_at": resets,
                                      "source": "test"}], now=at)
            rows = OddWeights.base + [fit("2026-11-07", 2.0)]
            (Path(tmp) / weights.FILENAME).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            d.housekeeping()
            self.assertEqual({f["invariant"] for f in ledger.findings("t")}, {"odd-weight", "off-schedule-reset"})


if __name__ == "__main__":
    unittest.main()
