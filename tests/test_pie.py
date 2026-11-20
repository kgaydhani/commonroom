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


def spend(samples, runs, until=100):
    return pie.estimate_spend(samples, runs, until=until * H)


class EstimateSpend(unittest.TestCase):
    def assertSpend(self, got, want):
        self.assertEqual(set(got), set(want))
        for name, value in want.items():
            self.assertAlmostEqual(got[name], value, places=9, msg=name)

    def test_growth_is_shared_by_expected_cost_in_the_gap(self):
        # The reading went 10 -> 16 somewhere between 1h and 2h. alpha costs $1/h (3$ over 3h), beta $2/h (6$ over
        # 3h), both running through the whole gap: $1 and $2 inside it, so 6 points split 1:2.
        samples = [sample(0, 1, 10), sample(2, 3, 16)]
        runs = [run("alpha", 0, 3, cost=3.0), run("beta", 0.5, 3.5, cost=6.0)]
        self.assertSpend(spend(samples, runs), {"alpha": 2.0, "beta": 4.0})

    def test_a_run_covering_part_of_the_gap_gets_its_part(self):
        # Same gap. alpha: $1/h for the whole hour, $1 inside it. beta: $2/h but only the gap's last minute, $1/30
        # inside it. 6 points split 1 : 1/30, so alpha 6 * 30/31 and beta 6 * 1/31.
        samples = [sample(0, 1, 10), sample(2, 3, 16)]
        runs = [run("alpha", 0, 3, cost=3.0), run("beta", 2 - 1 / 60, 3 - 1 / 60, cost=2.0)]
        self.assertSpend(spend(samples, runs), {"alpha": 180 / 31, "beta": 6 / 31})
        # beta ended exactly when the gap began: it gets nothing, whatever its rate.
        self.assertSpend(spend(samples, [run("alpha", 0, 3, cost=3.0), run("beta", 0, 1, cost=5.0)]), {"alpha": 6.0})

    def test_growth_is_shared_by_minutes_when_a_cost_is_missing(self):
        # Same gap (1h to 2h). beta is still running, so it has no cost yet: split by minutes inside the gap.
        # alpha ran all 60 of them, beta the last 30: 6 points split 2:1.
        samples = [sample(0, 1, 10), sample(2, 3, 16)]
        runs = [run("alpha", 0, 3, cost=3.0), run("beta", 1.5, None)]
        self.assertSpend(spend(samples, runs), {"alpha": 4.0, "beta": 2.0})

    def test_an_open_run_counts_only_up_to_until(self):
        # beta started at 1.25h and has no end; until is 1.5h, so it covers 15 minutes of the gap to alpha's 60:
        # 6 points split 4:1. It never counts for ever: with until at 1h it is not in the gap at all.
        samples = [sample(0, 1, 10), sample(2, 3, 16)]
        runs = [run("alpha", 0, 3, cost=3.0), run("beta", 1.25, None)]
        self.assertSpend(spend(samples, runs, until=1.5), {"alpha": 4.8, "beta": 1.2})
        self.assertSpend(spend(samples, [run("beta", 0.5, None)], until=1), {})

    def test_growth_with_no_run_active_is_nobodys(self):
        # 10 -> 13 between 1h and 2h with nothing running: the owner's own use. 13 -> 15 between 4h and 5h while
        # alpha runs: alpha's.
        samples = [sample(0, 1, 10), sample(2, 4, 13), sample(5, 6, 15)]
        runs = [run("alpha", 3.5, 5.5, cost=1.0)]
        self.assertSpend(spend(samples, runs), {"alpha": 2.0})
        self.assertEqual(spend(samples[:2], runs), {})

    def test_a_weekly_reset_does_not_create_spend(self):
        # 90 -> 2 across a reset, then 2 -> 5 in the new week: only the 3 points of the new week count. A rise across
        # a reset (5 -> 8 into the next window) is not spend either.
        samples = [sample(0, 1, 90), sample(2, 3, 2, RESET + pie.WEEK), sample(4, 5, 5, RESET + pie.WEEK)]
        runs = [run("alpha", 0, 6, cost=1.0)]
        self.assertSpend(spend(samples, runs), {"alpha": 3.0})
        rise = [sample(0, 1, 5), sample(2, 3, 8, RESET + pie.WEEK)]
        self.assertEqual(spend(rise, runs), {})

    def test_a_flapping_meter_counts_only_new_highs(self):
        # Two readers interleave: 16, 17, 16, 17, 18. The week grew 2 points (16 to 18), not 3.
        samples = [sample(0, 0.5, 16), sample(1, 1.5, 17), sample(2, 2.5, 16), sample(3, 3.5, 17), sample(4, 4.5, 18)]
        self.assertSpend(spend(samples, [run("alpha", 0, 5, cost=1.0)]), {"alpha": 2.0})
        # A fall of up to RESET_DROP points is flapping too: 52, 47, 53 grew 1.
        samples = [sample(0, 0.5, 52), sample(1, 1.5, 47), sample(2, 2.5, 53)]
        self.assertSpend(spend(samples, [run("alpha", 0, 5, cost=1.0)]), {"alpha": 1.0})

    def test_an_early_reset_inside_one_period_does_not_hide_growth(self):
        # 50, 52, 3, 4, 6 under one reset time: 2 points before the early reset and 3 after it, 5 in all.
        samples = [sample(0, 0.5, 50), sample(1, 1.5, 52), sample(2, 2.5, 3), sample(3, 3.5, 4), sample(4, 4.5, 6)]
        self.assertSpend(spend(samples, [run("alpha", 0, 5, cost=1.0)]), {"alpha": 5.0})

    def test_samples_with_one_timestamp_count_the_same_in_any_order(self):
        # After 18, three readings first seen at 2h: 19, 18, 19. However they arrive, the week grew 1 point between 1h and
        # 2h, so it goes to alpha, the run in that gap.
        tied = [sample(2, 2, 19), sample(2, 2, 18), sample(2, 2, 19)]
        for order in ([0, 1, 2], [1, 0, 2], [1, 2, 0], [2, 1, 0]):
            samples = [sample(0, 1, 18)] + [tied[i] for i in order]
            self.assertSpend(spend(samples, [run("alpha", 1.5, 2.5, cost=1.0), run("beta", 3, 4, cost=1.0)]),
                             {"alpha": 1.0})
        # An early reset and the reading before it share a timestamp: the one seen longer (3, until 3h) is the later.
        for samples in ([sample(0, 1, 50), sample(2, 2, 52), sample(2, 3, 3), sample(4, 5, 4)],
                        [sample(0, 1, 50), sample(2, 3, 3), sample(2, 2, 52), sample(4, 5, 4)]):
            self.assertSpend(spend(samples, [run("alpha", 0, 6, cost=1.0)]), {"alpha": 3.0})

    def test_the_projects_never_get_more_than_the_week_grew(self):
        rng = random.Random(225)
        for _ in range(50):
            used, samples, t = 10, [], 0.0
            for _ in range(40):
                used = max(0, used + rng.choice((-1, 1, 1, 2, -8)))
                samples.append(sample(t, t + rng.random(), used))
                t += 1 + rng.random()
            runs = [run(rng.choice(("alpha", "beta", "gamma")), s, s + rng.random() * 5,
                        rng.choice((None, rng.random()))) for s in (rng.random() * t for _ in range(10))]
            grew = spend(samples, [run("all", 0, None)], until=t)
            self.assertLessEqual(sum(spend(samples, runs, until=t).values()), grew.get("all", 0.0) + 1e-9)

    def test_negative_growth_counts_as_zero(self):
        self.assertEqual(spend([sample(0, 1, 10), sample(2, 3, 8)], [run("alpha", 0, 3, cost=1.0)]), {})

    def test_reset_times_that_jitter_within_the_hour_are_one_week(self):
        samples = [sample(0, 1, 10), sample(2, 3, 12, RESET + 600)]
        self.assertSpend(spend(samples, [run("alpha", 0, 3, cost=1.0)]), {"alpha": 2.0})


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
        name, why = pie.whose_turn(stands, {"alpha", "beta"})
        self.assertEqual(name, "beta")
        self.assertIn("furthest under its line", why)

    def test_a_project_over_its_line_runs_only_on_lent_share(self):
        # alpha is over (25 of 20). beta is under (2 of 10) with nothing ready. The projects spent 27 against lines
        # summing to 30, so alpha runs on beta's share.
        alpha, beta = Standing("alpha", 40, 25, 20), Standing("beta", 20, 2, 10)
        name, why = pie.whose_turn([alpha, beta], {"alpha"})
        self.assertEqual(name, "alpha")
        self.assertIn("lent by beta", why)
        # alpha 9 over (29 of 20): the projects spent 31 against 30, more than beta has left to lend.
        self.assertEqual(pie.whose_turn([Standing("alpha", 40, 29, 20), beta], {"alpha"})[0], None)
        # beta has work of its own: it goes, not alpha.
        self.assertEqual(pie.whose_turn([alpha, beta], {"alpha", "beta"})[0], "beta")
        # Nobody under its line: nothing to lend.
        self.assertEqual(pie.whose_turn([alpha, Standing("beta", 20, 12, 10)], {"alpha"})[0], None)

    def test_lending_does_not_depend_on_the_owners_own_use(self):
        # End of the week. alpha (40) spent 50, beta (40) spent 10 and is idle; the owner used his 20 himself, so the
        # plan reads 80. The projects spent 60 against lines of 80: alpha may run on beta's 30 unused points. The
        # owner's slice and the plan-wide limit are the gate's to enforce, and whose_turn is never told the reading.
        stands = pie.standings({"alpha": {"share": 40}, "beta": {"share": 40}}, {"alpha": 50, "beta": 10}, 1.0, 0)
        name, why = pie.whose_turn(stands, {"alpha"})
        self.assertEqual(name, "alpha")
        self.assertIn("lent by beta", why)

    def test_a_strict_projects_share_is_never_lent(self):
        alpha, beta = Standing("alpha", 40, 25, 20), Standing("beta", 20, 2, 10, strict=True)
        name, why = pie.whose_turn([alpha, beta], {"alpha"})
        self.assertIsNone(name)
        self.assertIn("over its line", why)
        # gamma (non-strict) has 1 point in hand. Lines sum to 40; beta's 8 unused points are held back, leaving 32.
        # The projects spent 25 + 2 + 9 = 36: anything alpha used now would come out of beta's share. Refused.
        gamma = Standing("gamma", 20, 9, 10)
        self.assertIsNone(pie.whose_turn([alpha, beta, gamma], {"alpha"})[0])
        # Had gamma spent nothing (27 spent, under 32), alpha would run on gamma's share.
        name, why = pie.whose_turn([alpha, beta, Standing("gamma", 20, 0, 10)], {"alpha"})
        self.assertEqual(name, "alpha")
        self.assertIn("lent by gamma", why)

    def test_a_spare_project_waits_for_paced_ones(self):
        # gamma is spare and further behind (nothing spent), but alpha is paced with work and headroom: alpha goes.
        alpha, gamma = Standing("alpha", 40, 10, 20), Standing("gamma", 20, 0, 10, "spare")
        self.assertEqual(pie.whose_turn([alpha, gamma], {"alpha", "gamma"})[0], "alpha")
        # With alpha idle, gamma runs on its own share.
        self.assertEqual(pie.whose_turn([alpha, gamma], {"gamma"})[0], "gamma")
        # alpha over its line and nothing to lend to it (gamma has work, so it lends nothing): gamma goes.
        over = Standing("alpha", 40, 25, 20)
        self.assertEqual(pie.whose_turn([over, gamma], {"alpha", "gamma"})[0], "gamma")

    def test_a_zero_share_never_runs(self):
        stands = [Standing("alpha", 0, 0, 0), Standing("beta", 20, 0, 10)]
        name, why = pie.whose_turn(stands, {"alpha"})
        self.assertIsNone(name)
        self.assertIn("share above zero", why)
        self.assertIsNone(pie.whose_turn([Standing("alpha", 0, 0, 0, "spare"), Standing("beta", 20, 0, 10)], {"alpha"})[0])

    def test_equal_standings_resolve_the_same_way_every_time(self):
        stands = [Standing(n, 20, 5, 10) for n in ("delta", "beta", "gamma", "alpha")]
        ready = {"alpha", "beta", "gamma", "delta"}
        first = pie.whose_turn(stands, ready)
        self.assertEqual(first[0], "alpha")
        rng = random.Random(225)
        for _ in range(20):
            rng.shuffle(stands)
            self.assertEqual(pie.whose_turn(stands, ready), first)

    def test_nobody_has_work(self):
        self.assertEqual(pie.whose_turn([Standing("alpha", 40, 0, 20)], set())[0], None)


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
