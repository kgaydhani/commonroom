"""The owner's absence lends the part of his slice he won't need (commonroom#255). Every test uses a Ledger and a
registry in a temporary room home through COMMONROOM_HOME, an empty transcripts folder, and no real session."""
import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from commonroom import away, cli, config, dispatch, owner_reserve, registry, standings, usage
from commonroom.registry import Project, Registry
from tests.test_dispatch import cfg_with
from tests.test_standings import H, Room

WEEK = 7 * 86400


class Away(Room):
    """alpha 30 and beta 55: the owner's slice is 15. The week resets in 60% of its length: 40% gone."""

    def setUp(self):
        super().setUp()
        owner_reserve._CACHE.clear()
        self.resets = self.now + 0.6 * WEEK
        self.active = mock.patch.object(usage, "owner_active", return_value=None)
        self.active.start()
        self.addCleanup(self.active.stop)
        reg = Registry({"alpha": Project("alpha", self.project_dir("alpha", "alpha-site"), 30),
                        "beta": Project("beta", self.project_dir("beta", "beta-site"), 55)})
        registry.save(reg)
        self.reading(self.now - 2 * H, 2)
        self.reading(self.now - 60, 3)
        for provider in ("claude", "codex"):
            self.ledger.record_usage([usage.observation(provider, "five_hour", 5, self.now + H, "test")],
                                     now=self.now - 60)

    def report(self):
        return standings.standings(self.ledger, registry.load(), "claude", self.now)

    def away_until(self, at):
        away.save(at, away.path(self.home))

    def test_return_at_70_percent_lends_10_5_in_proportion_to_the_shares(self):
        before = self.report()
        self.away_until(self.resets - 0.3 * WEEK)
        after = self.report()
        self.assertAlmostEqual(sum(after.lent.values()), 10.5)
        self.assertAlmostEqual(after.lent["alpha"], 10.5 * 30 / 85)   # 3.7
        self.assertAlmostEqual(after.lent["beta"], 10.5 * 55 / 85)    # 6.8
        self.assertAlmostEqual(after.owner_share, 4.5)
        for b, a in zip(before.rows, after.rows):
            self.assertAlmostEqual(a.share - b.share, after.lent[a.name])
            self.assertAlmostEqual(a.allowed - b.allowed, after.lent[a.name] * (0.4 + 0.08))   # its line moves with it

    def test_a_return_after_the_reset_lends_all_15(self):
        self.away_until(self.resets + 3600)
        report = self.report()
        self.assertAlmostEqual(sum(report.lent.values()), 15)
        self.assertAlmostEqual(report.owner_share, 0)

    def test_a_return_date_already_past_lends_nothing(self):
        self.away_until(self.now - 60)
        report = self.report()
        self.assertEqual((report.lent, report.away_until, report.owner_share), ({}, None, 15))

    def test_an_own_session_active_now_lends_nothing(self):
        self.away_until(self.resets - 0.3 * WEEK)
        self.active.stop()
        with mock.patch.object(usage, "owner_active", return_value="some-folder"):
            report = self.report()
        self.active.start()
        self.assertEqual((report.lent, report.away_until, report.owner_share), ({}, None, 15))

    def test_nothing_set_standings_and_gate_are_as_before(self):
        plain = self.report()
        self.assertEqual((plain.lent, plain.away_until), ({}, None))
        self.assertEqual([r.share for r in plain.rows], [30, 55])
        v = dispatch.verdicts(self.ledger, cfg_with(self.root / "work" / "alpha"), self.now)["claude"]
        self.assertNotIn("away_until", v.detail["owner_reserve"])

    def test_an_unreadable_file_changes_nothing(self):
        away.path(self.home).write_text("nonsense", encoding="utf-8")
        self.assertEqual(self.report().lent, {})

    def test_the_gate_reserve_drops_and_the_5_hour_reserve_and_owner_cap_do_not_apply(self):
        cfg = cfg_with(self.root / "work" / "alpha")
        base = dispatch.verdicts(self.ledger, cfg, self.now)["claude"]
        self.assertEqual(base.detail["owner_reserve"]["reserve_weekly"], 12)      # his slice 15, 3 used
        owner_reserve._CACHE.clear()
        self.away_until(self.resets - 0.3 * WEEK)
        v = dispatch.verdicts(self.ledger, cfg, self.now)["claude"]
        self.assertLess(v.detail["owner_reserve"]["reserve_weekly"], base.detail["owner_reserve"]["reserve_weekly"])
        self.assertEqual(v.detail["owner_reserve"]["away_until"], self.resets - 0.3 * WEEK)
        self.assertNotIn("owner_active", v.detail)

    def test_away_does_not_resume_a_paused_project(self):
        cfg = cfg_with(self.root / "work" / "alpha")
        self.ledger.trip("t", "owner", "paused by the owner", now=self.now - 5)
        self.away_until(self.resets + 3600)
        v = dispatch.verdicts(self.ledger, cfg, self.now)["claude"]
        self.assertFalse(v.allowed)
        self.assertTrue(self.ledger.open_breakers("t"))

    def test_an_empty_registry_changes_nothing(self):
        registry.save(Registry({}))
        self.away_until(self.resets + 3600)
        self.assertEqual(standings.standings(self.ledger, registry.load(), "claude", self.now).lent, {})


class Command(Room):
    def run_room(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_set_show_and_clear(self):
        self.assertEqual(self.run_room("away")[1].strip(), "not away")
        code, out, _ = self.run_room("away", "until", "2999-01-02 18:30")
        self.assertEqual(code, 0)
        self.assertIn("away until", out)
        self.assertEqual(json.loads(away.path().read_text())["until"], away.parse("2999-01-02 18:30"))
        self.assertIn("away until Thu 02 Jan 2999 18:30", self.run_room("away")[1].replace("Wed", "Thu"))
        self.assertEqual(self.run_room("away", "--clear")[1].strip(), "away cleared")
        self.assertFalse(away.path().exists())

    def test_a_past_or_unreadable_date_is_refused(self):
        self.assertEqual(self.run_room("away", "until", "2020-01-01")[0], 1)
        self.assertEqual(self.run_room("away", "until", "soon")[0], 1)
        self.assertFalse(away.path().exists())

    def test_pie_says_he_is_away_until_when_and_how_many_points(self):
        reg = Registry({"alpha": Project("alpha", self.project_dir("alpha", "alpha-site"), 30)})
        registry.save(reg)
        self.resets = self.now + 0.6 * WEEK
        self.reading(self.now - 60, 3)
        away.save(self.resets - 0.3 * WEEK, away.path(self.home))
        with mock.patch.object(usage, "owner_active", return_value=None):
            out = self.run_room("pie")[1]
            data = json.loads(self.run_room("pie", "--json")[1])
        self.assertIn("owner AWAY until", out)
        self.assertIn("points of his slice lent", out)
        self.assertGreater(data["providers"]["claude"]["lent_total"], 0)


if __name__ == "__main__":
    unittest.main()
