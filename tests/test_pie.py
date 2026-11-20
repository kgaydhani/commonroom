"""The pie arithmetic (commonroom#225). Every fixture is small enough to check by hand: times are in hours, readings
in percent of the weekly window."""
import ast
import random
import unittest
from pathlib import Path

from commonroom import pie
from commonroom.pie import Standing

H = 3600
RESET = 1000 * pie.WEEK        # the end of one weekly window; RESET + pie.WEEK ends the next


def sample(first_seen, observed_at, used, resets_at=RESET):
    """A reading first seen at `first_seen` hours and last seen with the same value at `observed_at` hours."""
    return {"first_seen": first_seen * H, "observed_at": observed_at * H, "used_pct": used, "resets_at": resets_at}


def run(project, start, end, cost=None):
    return {"project": project, "started_at": start * H, "ended_at": None if end is None else end * H, "cost_usd": cost}


class EstimateSpend(unittest.TestCase):
    def assertSpend(self, got, want):
        self.assertEqual(set(got), set(want))
        for name, value in want.items():
            self.assertAlmostEqual(got[name], value, places=9, msg=name)

    def test_growth_is_shared_by_cost_rate(self):
        # The reading went 10 -> 16 somewhere between 1h and 2h. alpha costs $1/h (3$ over 3h), beta $2/h (6$ over
        # 3h), both running through the gap: 6 points split 1:2.
        samples = [sample(0, 1, 10), sample(2, 3, 16)]
        runs = [run("alpha", 0, 3, cost=3.0), run("beta", 0.5, 3.5, cost=6.0)]
        self.assertSpend(pie.estimate_spend(samples, runs), {"alpha": 2.0, "beta": 4.0})

    def test_growth_is_shared_by_minutes_when_a_cost_is_missing(self):
        # Same gap (1h to 2h). beta is still running, so it has no cost yet: split by minutes inside the gap.
        # alpha ran all 60 of them, beta the last 30: 6 points split 2:1.
        samples = [sample(0, 1, 10), sample(2, 3, 16)]
        runs = [run("alpha", 0, 3, cost=3.0), run("beta", 1.5, None)]
        self.assertSpend(pie.estimate_spend(samples, runs), {"alpha": 4.0, "beta": 2.0})

    def test_growth_with_no_run_active_is_nobodys(self):
        # 10 -> 13 between 1h and 2h with nothing running: the owner's own use. 13 -> 15 between 4h and 5h while
        # alpha runs: alpha's.
        samples = [sample(0, 1, 10), sample(2, 4, 13), sample(5, 6, 15)]
        runs = [run("alpha", 3.5, 5.5, cost=1.0)]
        self.assertSpend(pie.estimate_spend(samples, runs), {"alpha": 2.0})
        self.assertEqual(pie.estimate_spend(samples[:2], runs), {})

    def test_a_weekly_reset_does_not_create_spend(self):
        # 90 -> 2 across a reset, then 2 -> 5 in the new week: only the 3 points of the new week count. A rise across
        # a reset (5 -> 8 into the next window) is not spend either.
        samples = [sample(0, 1, 90), sample(2, 3, 2, RESET + pie.WEEK), sample(4, 5, 5, RESET + pie.WEEK)]
        runs = [run("alpha", 0, 6, cost=1.0)]
        self.assertSpend(pie.estimate_spend(samples, runs), {"alpha": 3.0})
        rise = [sample(0, 1, 5), sample(2, 3, 8, RESET + pie.WEEK)]
        self.assertEqual(pie.estimate_spend(rise, runs), {})

    def test_negative_growth_counts_as_zero(self):
        self.assertEqual(pie.estimate_spend([sample(0, 1, 10), sample(2, 3, 8)], [run("alpha", 0, 3, cost=1.0)]), {})

    def test_reset_times_that_jitter_within_the_hour_are_one_week(self):
        samples = [sample(0, 1, 10), sample(2, 3, 12, RESET + 600)]
        self.assertSpend(pie.estimate_spend(samples, [run("alpha", 0, 3, cost=1.0)]), {"alpha": 2.0})


class Lines(unittest.TestCase):
    def test_a_line_is_share_times_elapsed_plus_its_slice_of_slack(self):
        # Half the week gone, share 40, plan slack 8: 40 * 0.5 + 8 * 40% = 20 + 3.2.
        self.assertAlmostEqual(pie.line(40, 0.5, 8), 23.2)
        self.assertEqual(pie.line(0, 0.5, 8), 0)

    def test_elapsed_fraction_of_the_week(self):
        self.assertEqual(pie.elapsed_fraction(RESET - pie.WEEK / 2, RESET), 0.5)
        self.assertEqual(pie.elapsed_fraction(RESET + 1, RESET), 1.0)
        self.assertEqual(pie.elapsed_fraction(RESET - 2 * pie.WEEK, RESET), 0.0)

    def test_standings(self):
        # Half the week gone, no slack. alpha (share 40) spent 25: line 20, headroom -5. beta (share 20) spent 2:
        # line 10, headroom 8.
        got = pie.standings({"beta": {"share": 20, "when": "spare", "strict": True}, "alpha": {"share": 40}},
                            {"alpha": 25.0, "beta": 2.0, "gamma": 9.0}, 0.5, 0)
        self.assertEqual(got, [Standing("alpha", 40, 25.0, 20.0), Standing("beta", 20, 2.0, 10.0, "spare", True)])
        self.assertEqual([s.headroom for s in got], [-5.0, 8.0])


class WhoseTurn(unittest.TestCase):
    def test_the_furthest_behind_for_its_size_goes_first(self):
        # alpha: 10 points in hand on a share of 40 (0.25 of its share). beta: 8 on a share of 20 (0.4). beta goes,
        # though alpha has more points in hand.
        stands = [Standing("alpha", 40, 10, 20), Standing("beta", 20, 2, 10)]
        name, why = pie.whose_turn(stands, {"alpha", "beta"}, 12)
        self.assertEqual(name, "beta")
        self.assertIn("furthest under its line", why)

    def test_a_project_over_its_line_runs_only_on_lent_share(self):
        # alpha is over (25 of 20). beta is under (2 of 10) with nothing ready. The lines sum to 30 and the plan is at
        # 27, so alpha runs on beta's share.
        alpha, beta = Standing("alpha", 40, 25, 20), Standing("beta", 20, 2, 10)
        name, why = pie.whose_turn([alpha, beta], {"alpha"}, 27)
        self.assertEqual(name, "alpha")
        self.assertIn("lent by beta", why)
        # The owner used 4 points himself: the plan (31) is over the lines (30), so nothing is spare to lend.
        self.assertEqual(pie.whose_turn([alpha, beta], {"alpha"}, 31)[0], None)
        # beta has work of its own: it goes, not alpha.
        self.assertEqual(pie.whose_turn([alpha, beta], {"alpha", "beta"}, 27)[0], "beta")
        # Nobody under its line: nothing to lend.
        self.assertEqual(pie.whose_turn([alpha, Standing("beta", 20, 12, 10)], {"alpha"}, 37)[0], None)

    def test_a_strict_projects_share_is_never_lent(self):
        alpha, beta = Standing("alpha", 40, 25, 20), Standing("beta", 20, 2, 10, strict=True)
        name, why = pie.whose_turn([alpha, beta], {"alpha"}, 27)
        self.assertIsNone(name)
        self.assertIn("over its line", why)
        # gamma (non-strict) has 1 point in hand. Lines sum to 40; beta's 8 unused points are held back, leaving 32.
        # The plan is at 25 + 2 + 9 = 36: anything alpha used now would come out of beta's share. Refused.
        gamma = Standing("gamma", 20, 9, 10)
        self.assertIsNone(pie.whose_turn([alpha, beta, gamma], {"alpha"}, 36)[0])
        # Had gamma spent nothing (plan at 27, under 32), alpha would run on gamma's share.
        name, why = pie.whose_turn([alpha, beta, Standing("gamma", 20, 0, 10)], {"alpha"}, 27)
        self.assertEqual(name, "alpha")
        self.assertIn("lent by gamma", why)

    def test_a_spare_project_waits_for_paced_ones(self):
        # gamma is spare and further behind (nothing spent), but alpha is paced with work and headroom: alpha goes.
        alpha, gamma = Standing("alpha", 40, 10, 20), Standing("gamma", 20, 0, 10, "spare")
        self.assertEqual(pie.whose_turn([alpha, gamma], {"alpha", "gamma"}, 10)[0], "alpha")
        # With alpha idle, gamma runs on its own share.
        self.assertEqual(pie.whose_turn([alpha, gamma], {"gamma"}, 10)[0], "gamma")
        # alpha over its line and nothing to lend to it (gamma has work, so it lends nothing): gamma goes.
        over = Standing("alpha", 40, 25, 20)
        self.assertEqual(pie.whose_turn([over, gamma], {"alpha", "gamma"}, 25)[0], "gamma")

    def test_a_zero_share_never_runs(self):
        stands = [Standing("alpha", 0, 0, 0), Standing("beta", 20, 0, 10)]
        name, why = pie.whose_turn(stands, {"alpha"}, 0)
        self.assertIsNone(name)
        self.assertIn("share above zero", why)
        self.assertIsNone(pie.whose_turn([Standing("alpha", 0, 0, 0, "spare"), Standing("beta", 20, 0, 10)], {"alpha"}, 0)[0])

    def test_equal_standings_resolve_the_same_way_every_time(self):
        stands = [Standing(n, 20, 5, 10) for n in ("delta", "beta", "gamma", "alpha")]
        ready = {"alpha", "beta", "gamma", "delta"}
        first = pie.whose_turn(stands, ready, 20)
        self.assertEqual(first[0], "alpha")
        rng = random.Random(225)
        for _ in range(20):
            rng.shuffle(stands)
            self.assertEqual(pie.whose_turn(stands, ready, 20), first)

    def test_nobody_has_work(self):
        self.assertEqual(pie.whose_turn([Standing("alpha", 40, 0, 20)], set(), 0)[0], None)


class Pure(unittest.TestCase):
    def test_pie_does_no_io_and_reads_no_clock(self):
        tree = ast.parse(Path(pie.__file__).read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertLessEqual(imported, {"__future__", "dataclasses"})
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertFalse(names & {"open", "print", "input"})


if __name__ == "__main__":
    unittest.main()
