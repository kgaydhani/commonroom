"""A project's dispatcher asks the pie before it starts model work (commonroom#240). Every test uses a Ledger and a
registry in a temporary room home through COMMONROOM_HOME, never ~/.commonroom, and an empty transcripts folder."""
import contextlib
import io
import unittest
from unittest import mock

from commonroom import cli, dispatch, gate, pie, registry, standings, usage
from commonroom.pie import Standing
from commonroom.registry import Project, Registry
from tests.test_dispatch import FakeBoard, cfg_with
from tests.test_standings import H, Room


class MayStart(unittest.TestCase):
    """Lines and spend in percent of the week. alpha and beta have share 40 and 20."""

    def test_under_its_line(self):
        stands = [Standing("alpha", 40, 10, 20), Standing("beta", 20, 1, 10)]
        ok, why = pie.may_start(stands, {"beta"}, "alpha")     # beta is active and further behind: alpha needn't wait
        self.assertTrue(ok)
        self.assertEqual(why, "alpha is under its line: 10.0% spent of 20.0% allowed, 10.0 points in hand on a share of 40")

    def test_over_its_line_with_a_lender(self):
        ok, why = pie.may_start([Standing("alpha", 40, 22, 20), Standing("beta", 20, 1, 10)], set(), "alpha")
        self.assertTrue(ok)
        self.assertIn("runs on share lent by beta", why)

    def test_over_its_line_with_no_lender(self):
        # beta is strict: its unused share is never lent.
        ok, why = pie.may_start([Standing("alpha", 40, 22, 20), Standing("beta", 20, 1, 10, strict=True)], set(), "alpha")
        self.assertFalse(ok)
        self.assertEqual(why, "alpha is over its line (22.0% spent of 20.0%): every project with work is over its line, "
                              "and no non-strict project under its line has unused share to lend while the projects' "
                              "spend is under the sum of their lines")

    def test_over_its_line_while_another_project_is_active_and_under_its_line(self):
        ok, why = pie.may_start([Standing("alpha", 40, 22, 20), Standing("beta", 20, 1, 10)], {"beta"}, "alpha")
        self.assertFalse(ok)
        self.assertTrue(why.startswith("alpha is over its line (22.0% spent of 20.0%): it is beta's turn, because "
                                       "beta (paced) is furthest under its line"), why)

    def test_spare_with_a_paced_project_active(self):
        stands = [Standing("alpha", 40, 1, 20, when="spare"), Standing("beta", 20, 1, 10)]
        ok, why = pie.may_start(stands, {"beta"}, "alpha")
        self.assertFalse(ok)
        self.assertIn("alpha is spare and runs only in allowance nobody else wants", why)
        self.assertIn("it is beta's turn", why)
        self.assertTrue(pie.may_start(stands, set(), "alpha")[0])       # nobody else wants it: spare runs

    def test_share_zero(self):
        stands = [Standing("alpha", 0, 0, 0), Standing("beta", 20, 1, 10)]
        self.assertEqual(pie.may_start(stands, set(), "alpha"), (False, "alpha has a share of zero, which is off"))

    def test_a_name_with_no_row_holds(self):
        self.assertEqual(pie.may_start([Standing("beta", 20, 1, 10)], set(), "alpha"),
                         (False, "alpha has no row in the pie"))

    def test_the_live_rooms_figures_of_20_nov(self):
        # The live room's standings on Claude on 20 Nov (a copy of its ledger, 27% of the week gone): commonroom
        # (share 55) 7.7 points under its line, magazine (share 30) 1.9 over, neither active.
        live = [Standing("commonroom", 55, 11.50, 19.23), Standing("magazine", 30, 12.36, 10.49)]
        self.assertTrue(pie.may_start(live, {"magazine"}, "commonroom")[0])
        ok, why = pie.may_start(live, {"commonroom"}, "magazine")
        self.assertFalse(ok)
        self.assertIn("it is commonroom's turn", why)
        # With commonroom idle, its unused share is lent while the two together are under the sum of their lines.
        ok, why = pie.may_start(live, set(), "magazine")
        self.assertTrue(ok, why)
        self.assertIn("lent by commonroom", why)


class Gate(Room):
    """The fixture's week (see tests/test_standings.py): alpha-site spent 4.5, beta-site 1.5; half the week gone, so
    a line is share x 0.58 with Claude's slack of 8. alpha at share 40 has line 23.2 (under); at share 5, 2.9 (over).
    The dispatcher is alpha's."""

    def setUp(self):
        super().setUp()
        self.week()
        for provider in ("claude", "codex"):
            self.ledger.record_usage([usage.observation(provider, "five_hour", 5, self.now + H, "test"),
                                      usage.observation(provider, "seven_day", 20, self.resets, "test")], now=self.now)
        self.dirs = {key: self.project_dir(key, f"{key}-site") for key in ("alpha", "beta", "gamma")}
        cfg = cfg_with(self.dirs["alpha"])
        cfg["project"]["name"] = "alpha-site"
        self.cfg = cfg
        self.d = self.dispatcher(cfg)

    def dispatcher(self, cfg):
        d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None)
        d.claude_projects = self.transcripts
        return d

    def projects(self, **shares):
        """Register each key with its share, or (share, when, strict)."""
        reg = Registry()
        for key, spec in shares.items():
            share, when, strict = spec if isinstance(spec, tuple) else (spec, "paced", False)
            reg.projects[key] = Project(key, self.dirs[key], share, when=when, strict=strict)
        registry.save(reg)

    def beta_active(self):
        self.add_run("beta-site", self.now - 600)          # running now, from this (live) process

    def claude(self):
        return self.d.allowed(self.now)["claude"]

    def assertHeld(self, v, *parts):
        self.assertFalse(v.allowed, v.reason)
        self.assertFalse(v.stop_running)
        self.assertTrue(v.reason.startswith("claude: "), v.reason)
        for part in parts:
            self.assertIn(part, v.reason)

    def test_with_no_registry_the_gate_alone_decides(self):
        self.assertEqual((self.claude().allowed, self.claude().reason), (True, "claude: allowed"))

    def test_under_its_line_it_starts_while_another_project_under_its_line_runs(self):
        self.projects(alpha=40, beta=20)
        self.beta_active()
        self.assertEqual((self.claude().allowed, self.claude().reason), (True, "claude: allowed"))

    def test_over_its_line_it_borrows_from_an_idle_project(self):
        self.projects(alpha=5, beta=20)
        self.assertTrue(self.claude().allowed, self.claude().reason)

    def test_over_its_line_it_waits_while_a_project_under_its_line_is_active(self):
        self.projects(alpha=5, beta=20)
        self.beta_active()
        v = self.claude()
        self.assertHeld(v, "the pie holds new work: alpha is over its line (4.5% spent of 2.9%): it is beta's turn")
        self.assertIsNone(self.d.stop_reason("claude"))     # it holds new starts only: a running worker carries on

    def test_a_run_that_ended_within_pie_idle_minutes_is_active(self):
        self.projects(alpha=5, beta=20)
        self.add_run("beta-site", self.now - 50 * 60, self.now - 30 * 60, cost=1)
        self.assertHeld(self.claude(), "it is beta's turn")
        with mock.patch.object(dispatch, "PIE_IDLE_MINUTES", 20):
            self.assertTrue(self.claude().allowed)

    def test_over_its_line_with_no_lender_it_waits(self):
        self.projects(alpha=5, beta=(20, "paced", True))
        self.assertHeld(self.claude(), "alpha is over its line", "no non-strict project under its line")

    def test_spare_waits_while_a_paced_project_is_active(self):
        self.projects(alpha=(40, "spare", False), beta=20)
        self.assertTrue(self.claude().allowed)
        self.beta_active()
        self.assertHeld(self.claude(), "alpha is spare", "it is beta's turn")

    def test_share_zero_holds(self):
        self.projects(alpha=0, beta=20)
        self.assertHeld(self.claude(), "alpha has a share of zero")

    def test_a_registry_without_this_project_holds_and_names_room_projects_add(self):
        self.projects(beta=20, gamma=20)
        self.assertHeld(self.claude(), "is not in the project registry", "`room projects add`")
        self.assertFalse(self.d.allowed(self.now)["codex"].allowed)

    def test_a_registry_that_cant_be_read_holds_with_its_error(self):
        (self.home / registry.FILENAME).write_text('[projects.alpha]\npath = "nowhere"\nshare = 140\n', encoding="utf-8")
        self.assertHeld(self.claude(), "the project registry can't be read", "'share' must be a number from 0 to 100")

    def test_standings_that_cant_be_computed_hold(self):
        self.projects(alpha=40, beta=20)
        with mock.patch.object(dispatch.standings_mod, "standings", return_value=None):
            self.assertHeld(self.claude(), "the pie has no current weekly reading", "standing is unknown")
        with mock.patch.object(dispatch.standings_mod, "standings", side_effect=OSError("transcripts unreadable")):
            self.assertHeld(self.claude(), "the pie's standings can't be computed", "transcripts unreadable")

    def test_a_registered_project_is_nobodys_guest(self):
        cfg = dict(self.cfg, share={"max": 0.1, "hours": 24, "idle_minutes": 60})
        self.d = self.dispatcher(cfg)
        self.beta_active()
        self.assertIn("has had its share", self.claude().reason)        # today's [share], with no registry
        self.projects(alpha=40, beta=20)
        self.assertEqual((self.claude().allowed, self.claude().reason), (True, "claude: allowed"))

    def test_the_registry_is_read_on_every_pass(self):
        self.projects(alpha=0, beta=20)
        self.assertFalse(self.claude().allowed)
        self.projects(alpha=40, beta=20)                    # the owner's edit, with the same dispatcher
        self.assertTrue(self.claude().allowed)

    def test_standings_are_computed_once_per_allowed_provider_per_pass(self):
        self.projects(alpha=40, beta=20)
        # The gate's owner reserve (commonroom#241) makes its own estimate once per meter reading: let it, first.
        self.d.allowed(self.now)
        with mock.patch.object(dispatch.standings_mod, "standings", wraps=standings.standings) as calls:
            result = self.d.allowed(self.now)
        self.assertEqual(sorted(c.args[2] for c in calls.call_args_list), sorted(p for p, v in result.items() if v.allowed))
        held = {"claude": gate.Verdict(False, "claude: paused"), "codex": gate.Verdict(False, "codex: paused")}
        with mock.patch.object(dispatch.standings_mod, "standings") as calls:
            self.assertEqual(dispatch.pie_holds(self.ledger, self.cfg, held, self.now), {})
        calls.assert_not_called()                           # the gate already holds: the pie isn't asked

    def room(self, command, cfg):
        """`room <command>` run as alpha's: its own config, while each registered project's room.toml is still read."""
        load, out = cli.config_mod.load, io.StringIO()

        def as_alpha(path=None, *args, **kwargs):
            return cfg if path is None else load(path, *args, **kwargs)

        with mock.patch.object(cli.config_mod, "load", side_effect=as_alpha), contextlib.redirect_stdout(out):
            cli.main([command])
        return out.getvalue()

    def test_room_usage_and_status_show_the_pies_reason(self):
        self.projects(alpha=0, beta=20)
        cfg = dict(self.cfg, project=dict(self.cfg["project"], repo=None))     # no board to read
        for command in ("usage", "status"):
            with self.subTest(command=command):
                self.assertIn("claude: HOLD - claude: the pie holds new work: alpha has a share of zero",
                              self.room(command, cfg))


if __name__ == "__main__":
    unittest.main()
