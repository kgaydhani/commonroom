"""Every weekly window with a projection, and whether a move off one worked (commonroom#256). Every ledger lives in a
temporary room home through COMMONROOM_HOME (tests.test_standings.Room), never ~/.commonroom."""
import contextlib
import io
import json
import unittest
from unittest import mock

from commonroom import cli, gate, pie, standings, usage, weekly
from tests.test_standings import H, Room

DAY = 86400
TOP = "seven_day_fable"


def row(first, pct, resets, observed=None):
    return {"first_seen": first, "observed_at": observed if observed is not None else first, "used_pct": pct,
            "resets_at": resets}


class Projection(unittest.TestCase):
    now, resets = 1_000_000.0, 1_000_000.0 + 5 * DAY

    def test_two_windows_one_lasts_the_week_and_the_top_model_runs_out(self):
        # All models 40 -> 45 over the day: 5 a day, 55 left needs 11 days. Top model 60 -> 72: 12 a day, 28 left.
        week = weekly.projection("seven_day", [row(self.now - DAY, 40, self.resets), row(self.now - 3 * H, 45, self.resets)],
                                 self.resets, self.now)
        top = weekly.projection(TOP, [row(self.now - DAY, 60, self.resets), row(self.now - 3 * H, 72, self.resets)],
                                self.resets, self.now)
        self.assertAlmostEqual(week.rate_per_day, 5)
        self.assertTrue(week.lasts)
        self.assertAlmostEqual(top.rate_per_day, 12)
        self.assertAlmostEqual(top.runs_out_at, self.now + 28 / 12 * DAY)
        self.assertFalse(top.lasts)

    def test_the_rate_is_the_last_24_hours_only(self):
        rows = [row(self.now - 3 * DAY, 10, self.resets), row(self.now - 2 * DAY, 50, self.resets),
                row(self.now - H, 52, self.resets)]
        self.assertAlmostEqual(weekly.projection(TOP, rows, self.resets, self.now).rate_per_day, 2)

    def test_too_few_readings_give_no_projection(self):
        p = weekly.projection(TOP, [row(self.now - 600, 5, self.resets)], self.resets, self.now)
        self.assertEqual(p.used_pct, 5)
        self.assertIsNone(p.rate_per_day)
        self.assertFalse(p.lasts)

    def test_a_reading_of_the_last_period_is_not_used(self):
        old = row(self.now - DAY, 90, self.resets - 7 * DAY)
        p = weekly.projection(TOP, [old, row(self.now - 2 * H, 1, self.resets), row(self.now - H, 3, self.resets)],
                              self.resets, self.now)
        self.assertAlmostEqual(p.rate_per_day, 2 / 2 * 24)
        self.assertEqual(p.used_pct, 3)

    def test_the_window_name_carries_the_model(self):
        self.assertEqual(weekly.model_part(TOP), "fable")
        self.assertIsNone(weekly.model_part("seven_day"))
        self.assertEqual(weekly.window_for("claude-fable-5-2", ["seven_day", TOP]), TOP)
        self.assertIsNone(weekly.window_for("claude-sonnet-5-5", ["seven_day", TOP]))
        self.assertIsNone(weekly.window_for("sonnet", ["seven_day"]))


class SwitchCheck(unittest.TestCase):
    resets = 10 * DAY

    def histories(self, top, week, at=1000.0, after=5000.0):
        return {TOP: [row(0, top[0], self.resets), row(after, top[1], self.resets)],
                "seven_day": [row(0, week[0], self.resets), row(after, week[1], self.resets)]}

    def test_the_switch_worked_when_the_top_window_stops_and_the_other_rises(self):
        s = weekly.check_switch(self.histories((70, 70), (45, 47)), TOP, 1000.0, 6000.0)
        self.assertEqual(s.outcome, "worked")
        self.assertEqual((s.before, s.after), ({TOP: 70, "seven_day": 45}, {TOP: 70, "seven_day": 47}))

    def test_the_switch_changed_nothing_when_the_top_window_still_rises(self):
        self.assertEqual(weekly.check_switch(self.histories((70, 73), (45, 48)), TOP, 1000.0, 6000.0).outcome,
                         "changed nothing")

    def test_no_reading_since_the_switch_says_so(self):
        h = {TOP: [row(0, 70, self.resets, observed=900)], "seven_day": [row(0, 45, self.resets, observed=900)]}
        self.assertEqual(weekly.check_switch(h, TOP, 1000.0, 6000.0).outcome, "no reading since")

    def test_the_room_moved_off_a_window_when_its_last_run_on_it_ended_and_another_model_ran_after(self):
        runs = [{"model": "fable", "started_at": 0, "ended_at": 100}, {"model": "sonnet", "started_at": 150, "ended_at": None}]
        self.assertEqual(weekly.room_switch(runs, ["seven_day", TOP]), (TOP, 100))
        runs.append({"model": "fable", "started_at": 200, "ended_at": None})
        self.assertIsNone(weekly.room_switch(runs, ["seven_day", TOP]))      # back on it: no move
        self.assertIsNone(weekly.room_switch(runs[:1], ["seven_day", TOP]))  # nothing ran after


class FromTheLedger(Room):
    registered = False

    def register(self, **kw):
        self.registered = True
        return super().register(**kw)

    def read(self, at, windows, source="usage-screen"):
        self.ledger.record_usage([usage.observation("claude", w, pct, self.resets, source) for w, pct in windows.items()],
                                 now=at)

    def test_every_weekly_window_is_listed_the_all_models_week_first(self):
        self.read(self.now - DAY, {"five_hour": 3, "seven_day": 40, TOP: 60})
        self.read(self.now - H, {"five_hour": 9, "seven_day": 45, TOP: 72})
        found = weekly.windows(self.ledger, "claude", self.now)
        self.assertEqual([p.window for p in found], ["seven_day", TOP])
        self.assertEqual([p.used_pct for p in found], [45, 72])

    def test_a_top_model_window_missing_from_the_latest_reading_is_unknown_not_free(self):
        self.read(self.now - 2 * H, {"five_hour": 3, "seven_day": 40, TOP: 70})
        self.read(self.now - H, {"five_hour": 4, "seven_day": 41})
        found = {p.window: p for p in weekly.windows(self.ledger, "claude", self.now)}
        self.assertNotIn(TOP, self.ledger.latest_usage("claude"))
        self.assertFalse(found[TOP].known)
        self.assertIsNone(found[TOP].used_pct)
        self.assertTrue(found["seven_day"].known)

    def pie_out(self, as_json=False):
        if not self.registered:
            self.register()
        out = io.StringIO()
        with mock.patch("time.time", return_value=self.now), contextlib.redirect_stdout(out):
            cli.cmd_pie(mock.Mock(json=as_json))
        return out.getvalue()

    def test_room_pie_shows_each_window_and_that_the_switch_worked(self):
        self.read(self.now - DAY, {"five_hour": 3, "seven_day": 40, TOP: 60})
        self.add_run("alpha-site", self.now - 5 * H, self.now - 4 * H, model="fable")
        self.add_run("alpha-site", self.now - 3 * H, self.now - 2 * H, model="sonnet")
        self.read(self.now - 4.5 * H, {"five_hour": 6, "seven_day": 43, TOP: 66})
        self.read(self.now - H, {"five_hour": 9, "seven_day": 45, TOP: 66})
        out = self.pie_out()
        self.assertIn("week (all models): 45% used", out)
        self.assertIn("week (fable): 66% used", out)
        self.assertIn(f"switch: the room's work left {TOP}", out)
        self.assertIn("WORKED", out)
        data = json.loads(self.pie_out(as_json=True))
        self.assertEqual(data["weekly"]["claude"]["switch"]["outcome"], "worked")

    def test_with_one_weekly_window_nothing_else_changes(self):
        # Codex, or a plan with no per-model limit: one window, no switch, and the same standings and gate verdict as
        # with a per-model window beside it.
        self.week()
        reg = self.register()
        before = standings.standings(self.ledger, reg, "claude", self.now)
        found = weekly.windows(self.ledger, "claude", self.now)
        self.assertEqual([p.window for p in found], ["seven_day"])
        self.assertIsNone(weekly.switch(self.ledger, "claude", self.now, found))
        policy = {"reserve_weekly": 10, "reserve_5h": 5, "pace": True}
        self.ledger.record_usage([usage.observation("claude", "five_hour", 5, self.now + H, "test")], now=self.now - H)
        verdict = gate.usage_verdict("claude", self.ledger.latest_usage("claude"), policy, self.now, 3600)
        self.ledger.record_usage([usage.observation("claude", TOP, 90, self.resets, "test")], now=self.now - H)
        self.assertEqual(standings.standings(self.ledger, reg, "claude", self.now), before)
        self.assertEqual(gate.usage_verdict("claude", self.ledger.latest_usage("claude"), policy, self.now, 3600), verdict)
        out = self.pie_out()
        self.assertNotIn("switch:", out)


if __name__ == "__main__":
    unittest.main()
