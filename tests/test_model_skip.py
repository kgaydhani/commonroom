"""The room's own work keeps off a model whose own weekly window is past its line (commonroom#256, part 3). Every
ledger is in a temporary folder, never ~/.commonroom."""
import tempfile
import time
import unittest
from pathlib import Path

from commonroom import dispatch, gate, usage, weekly
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with

DAY = 86400
TOP = "seven_day_fable"
BUDGET = {"reserve_weekly": 25, "weekly_slack": 8}
SPEC = ["claude:fable", "claude:sonnet"]
ALLOWED = {"claude": gate.Verdict(True, "claude: allowed")}


class Line(unittest.TestCase):
    now = 1_000_000.0
    resets = now + 5 * DAY   # two days into the week: the line is 75 * 2/7 + 8, about 29%

    def p(self, window, used):
        return weekly.Projection(window, used, self.resets, self.now, None, None)

    def test_a_window_past_its_line_is_held_and_one_under_it_is_not(self):
        self.assertAlmostEqual(weekly.line(self.p(TOP, 0), BUDGET, self.now), 75 * 2 / 7 + 8)
        held = weekly.held([self.p("seven_day", 45), self.p(TOP, 72)], BUDGET, self.now)
        self.assertEqual(list(held), [TOP])
        self.assertIn("past its line (72% used, the line is 29% by now)", held[TOP])
        self.assertEqual(weekly.held([self.p("seven_day", 45), self.p(TOP, 20)], BUDGET, self.now), {})

    def test_the_all_models_week_is_never_held_here(self):
        self.assertEqual(weekly.held([self.p("seven_day", 90)], BUDGET, self.now), {})   # that is the gate's

    def test_an_unknown_window_is_held(self):
        held = weekly.held([self.p("seven_day", 45), self.p(TOP, None)], BUDGET, self.now)
        self.assertIn("unknown", held[TOP])

    def test_the_skip_follows_the_model_named_by_the_window(self):
        found = [self.p("seven_day", 45), self.p(TOP, 72)]
        self.assertIn(TOP, weekly.skip_reason("fable", found, BUDGET, self.now))
        self.assertIn(TOP, weekly.skip_reason("claude-fable-5-2", found, BUDGET, self.now))
        self.assertIsNone(weekly.skip_reason("sonnet", found, BUDGET, self.now))
        self.assertIsNone(weekly.skip_reason(None, found, BUDGET, self.now))


class Choose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        self.now = time.time()
        self.resets = self.now + 5 * DAY
        self.logged = []
        self.d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard([]), log=self.logged.append)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def read(self, windows: dict, ago: float = 0, source: str = "usage-screen"):
        obs = [usage.observation("claude", w, pct, self.resets, source) for w, pct in windows.items()]
        obs.append(usage.observation("claude", "five_hour", 10, self.now + 3600, source))
        self.ledger.record_usage(obs, now=self.now - ago)

    def test_a_model_past_its_line_is_skipped_for_the_next_in_the_list_and_logged_once(self):
        self.read({"seven_day": 45, TOP: 72})
        self.assertEqual(self.d.choose(SPEC, ALLOWED), ("claude", "sonnet"))
        self.assertEqual(self.d.choose(SPEC, ALLOWED), ("claude", "sonnet"))
        self.assertEqual(len(self.logged), 1)
        self.assertIn("claude:fable skipped for the next model in the role's list: seven_day_fable is past its line",
                      self.logged[0])

    def test_no_skip_while_the_window_is_under_its_line(self):
        self.read({"seven_day": 45, TOP: 20})
        self.assertEqual(self.d.choose(SPEC, ALLOWED), ("claude", "fable"))
        self.assertEqual(self.logged, [])

    def test_with_one_weekly_window_everything_is_as_before(self):
        # A plan without per-model limits (or Codex): even a full-looking week changes no model choice here.
        self.read({"seven_day": 72})
        for spec in (SPEC, ["claude:sonnet"], ["codex", "claude:fable"], ["claude"]):
            self.assertEqual(self.d.choose(spec, ALLOWED), dispatch.choose_provider(spec, ALLOWED))
        self.assertEqual(self.logged, [])

    def test_a_list_with_only_the_held_model_runs_nothing(self):
        self.read({"seven_day": 45, TOP: 72})
        self.assertIsNone(self.d.choose(["claude:fable"], ALLOWED))

    def test_a_window_missing_from_the_latest_reading_counts_as_unknown_never_free(self):
        self.read({"seven_day": 40, TOP: 10}, ago=2 * 3600)
        self.read({"seven_day": 41}, ago=3600)          # the reader left the per-model window out of this one
        self.assertNotIn(TOP, self.ledger.latest_usage("claude"))
        self.assertEqual(self.d.choose(SPEC, ALLOWED), ("claude", "sonnet"))
        self.assertIn("unknown", self.logged[0])
        cfg = self.d.cfg
        cfg["audit"] = {"every_hours": 1}
        run = self.ledger.start_run("t", "#1", "builder", "claude")
        self.ledger.finish_run(run, "done", outputs=[{"kind": "pr", "ref": "PR #2"}])
        self.assertIsNone(self.d.audit_due())           # the audit holds on it too

    def test_the_audit_runs_under_the_owners_quarter_and_holds_past_it(self):
        self.d.cfg["audit"] = {"every_hours": 1}
        run = self.ledger.start_run("t", "#1", "builder", "claude")
        self.ledger.finish_run(run, "done", outputs=[{"kind": "pr", "ref": "PR #2"}])
        self.read({"seven_day": 45, TOP: 75})
        self.assertIn("periodic audit", self.d.audit_due())
        self.read({"seven_day": 45, TOP: 76})
        self.assertIsNone(self.d.audit_due())
        self.read({"seven_day": 90})                    # one weekly window only: the all-models week is the gate's
        self.ledger.db.execute("DELETE FROM usage WHERE window=?", (TOP,)); self.ledger.db.commit()
        self.assertIn("periodic audit", self.d.audit_due())


if __name__ == "__main__":
    unittest.main()
