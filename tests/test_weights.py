"""`room weights` (commonroom#644). Made-up rows and readings, in temporary folders."""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from commonroom import cli, weights
from commonroom.ledger import Ledger

DAY = 86400.0
T0 = 1_760_000_000.0 - (1_760_000_000.0 % DAY)
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def day(n):
    return datetime.fromtimestamp(T0 + n * DAY, timezone.utc).strftime("%Y-%m-%d")


def row(n, model, dollars):
    """A row costing `dollars` at API prices: input tokens only (opus $4 per M, sonnet $2 per M)."""
    per_m = 4.0 if model == OPUS else 2.0
    return {"day": day(n), "model": model, "input": round(dollars / per_m * 1e6), "output": 0, "cache_write": 0, "cache_read": 0}


def synthetic(days=10, opus_per_point=1.0, sonnet_per_point=2.0, mix=lambda n: (3 + n % 4, 5 - n % 3)):
    """Rows and readings where opus costs `opus_per_point` dollars a point and sonnet `sonnet_per_point`, climbing one
    reading a day, one period, no resets."""
    rows, readings, pct = [], [], 0.0
    for n in range(days):
        a, b = mix(n)                                       # points each model's work moved the meter
        rows += [row(n, OPUS, a * opus_per_point), row(n, SONNET, b * sonnet_per_point)]
        pct += a + b
        readings.append({"first_seen": T0 + n * DAY + 3600, "observed_at": T0 + n * DAY + 3600, "used_pct": pct,
                         "resets_at": T0 + 30 * DAY})
    return rows, [{"first_seen": T0, "observed_at": T0, "used_pct": 0.0, "resets_at": T0 + 30 * DAY}] + readings


class Fit(unittest.TestCase):
    def test_recovers_one_to_two(self):
        rows, readings = synthetic()
        r = weights.fit(rows, readings, "seven_day", today=day(11))
        self.assertTrue(r["ok"], r)
        o, s = r["models"]["opus"]["dollars_per_point"], r["models"]["sonnet"]["dollars_per_point"]
        self.assertAlmostEqual(o, 1.0, delta=0.02)
        self.assertAlmostEqual(s, 2.0, delta=0.04)
        self.assertAlmostEqual(s / o, 2.0, delta=0.05)
        self.assertLess(r["spread"], 0.02)

    def test_noise_is_spread_not_a_different_answer(self):
        rows, readings = synthetic(days=14)
        for i, r in enumerate(rows):
            r["input"] = round(r["input"] * (1.02 if i % 4 < 2 else 0.98))
        r = weights.fit(rows, readings, "seven_day", today=day(15))
        self.assertAlmostEqual(r["models"]["sonnet"]["dollars_per_point"] / r["models"]["opus"]["dollars_per_point"], 2.0, delta=0.3)
        self.assertGreater(r["spread"], 0.01)

    def test_early_reset_day_is_split_out(self):
        rows, readings = synthetic()
        # day 5: the meter falls to 1 long before its reset time, then climbs; that day's cost would mislead the fit
        fall = {"first_seen": T0 + 5 * DAY + 100, "observed_at": T0 + 5 * DAY + 100, "used_pct": 1.0, "resets_at": T0 + 30 * DAY}
        readings = sorted(readings + [fall], key=lambda r: r["first_seen"])
        rises, resets = weights.daily_rises(readings)
        self.assertEqual(resets, [day(5)])
        r = weights.fit(rows, readings, "seven_day", today=day(11))
        self.assertTrue(r["ok"])
        self.assertEqual(r["reset_days"], [day(5)])
        self.assertEqual(r["days"], 9)

    def test_natural_rollover_counts_from_zero(self):
        a = {"first_seen": T0, "observed_at": T0, "used_pct": 50.0, "resets_at": T0 + 3600}
        b = {"first_seen": T0 + 3700, "observed_at": T0 + 3700, "used_pct": 0.0, "resets_at": T0 + 3600 + 5 * 3600}
        c = {"first_seen": T0 + 4000, "observed_at": T0 + 4000, "used_pct": 4.0, "resets_at": T0 + 3600 + 5 * 3600}
        rises, resets = weights.daily_rises([a, b, c])
        self.assertEqual((rises[day(0)], resets), (4.0, []))

    def test_too_little_data_says_what_is_missing(self):
        rows, readings = synthetic(days=3)
        r = weights.fit(rows, readings, "seven_day", today=day(4))
        self.assertFalse(r["ok"])
        self.assertNotIn("models", r)
        self.assertIn("usable days of", " ".join(r["missing"]))
        none = weights.fit([], [], "five_hour", today=day(4))
        self.assertFalse(none["ok"])
        self.assertEqual(len(none["missing"]), 2)
        self.assertIn("no five_hour readings", none["missing"][0])
        self.assertIn("token rows", none["missing"][1])

    def test_today_and_unpriced_models_are_left_out(self):
        rows, readings = synthetic()
        rows.append({"day": day(2), "model": "some-new-model", "input": 10**9, "output": 0, "cache_write": 0, "cache_read": 0})
        r = weights.fit(rows, readings, "seven_day", today=day(9))
        self.assertEqual(r["days"], 9)                          # day 9 is "today"
        self.assertEqual(sorted(r["models"]), ["opus", "sonnet"])


class Command(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def room(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["weights", *args]), 0)
        return buf.getvalue()

    def seed(self, rows, readings):
        import json
        (self.home / "token-rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        ledger = Ledger()
        for r in readings:
            ledger.db.execute("INSERT INTO usage (observed_at, first_seen, provider, window, used_pct, resets_at, source) "
                              "VALUES (?,?,?,?,?,?,?)", (r["observed_at"], r["first_seen"], "claude", "seven_day", r["used_pct"], r["resets_at"], "test"))
        ledger.db.commit()
        ledger.db.close()

    def test_record_stores_dated_fits_and_show_prints_them(self):
        rows, readings = synthetic(days=12)
        self.seed(rows, readings)
        with mock.patch.object(weights, "utc_today", return_value=day(13)):
            out = self.room("record")
            self.room("record")                                  # the same day again replaces, never doubles
        hist = weights.read_history(self.home / weights.FILENAME)
        self.assertEqual([(h["date"], h["meter"]) for h in hist], [(day(13), "seven_day")])
        self.assertAlmostEqual(hist[0]["models"]["opus"]["dollars_per_point"], 1.0, delta=0.02)
        self.assertIn("latest " + day(13), out)
        self.assertIn("opus $1.0", out)
        self.assertIn("five_hour:\n  not enough data: no five_hour readings", out)
        shown = self.room()                                      # plain `weights` only reads
        self.assertIn("latest " + day(13), shown)
        self.assertEqual(len(weights.read_history(self.home / weights.FILENAME)), 1)

    def test_history_shows_each_day(self):
        for d in (10, 11):
            weights.record({"ok": True, "date": day(d), "meter": "seven_day", "days": 8, "spread": 0.05, "reset_days": [], "left_out": [],
                            "models": {"opus": {"dollars_per_point": 1.0 + d / 100, "spread": 0.02}}})
        text = "\n".join(weights.report(weights.read_history(), []))
        self.assertIn("latest " + day(11), text)
        self.assertIn(f"    {day(10)}: opus $1.10", text)
        self.assertFalse(weights.record({"ok": False, "meter": "seven_day", "missing": ["x"]}))
        self.assertEqual(len(weights.read_history()), 2)


if __name__ == "__main__":
    unittest.main()
