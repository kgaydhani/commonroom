"""For a project in the pie's registry, the gate's weekly reserve is the owner's unused slice, never under the room's
`owner_floor_weekly` (commonroom#241). Every test uses a Ledger and a registry in a temporary room home through
COMMONROOM_HOME, never ~/.commonroom, and an empty transcripts folder."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import config, dispatch, gate, owner_reserve, policy, registry, standings, usage
from commonroom.registry import Project, Registry
from tests.test_dispatch import FakeBoard, cfg_with
from tests.test_standings import H, Room


class Reserve(unittest.TestCase):
    def test_the_unused_slice_and_never_under_the_floor(self):
        self.assertEqual(owner_reserve.FLOOR, 5)
        self.assertEqual(owner_reserve.reserve(15, 10), 5)         # slice 15, 10 used: 5 unused
        self.assertEqual(owner_reserve.reserve(15, 5), 10)
        self.assertEqual(owner_reserve.reserve(15, 40), 5)         # 0 unused: the floor
        self.assertEqual(owner_reserve.reserve(15, 24.1), 5)       # the live room on 20 Nov: slice 15, own use 24.1
        self.assertEqual(owner_reserve.reserve(15, 5.64), 9.4)     # a tenth of a point
        self.assertIs(type(owner_reserve.reserve(15, 5)), int)     # so the gate's reason reads "10% kept back"

    def test_the_floor_is_a_room_wide_policy_value_with_a_floor_and_ceiling(self):
        self.assertEqual((policy.DEFAULTS["owner_floor_weekly"], policy.BOUNDS["owner_floor_weekly"]), (5, (1, 50)))
        self.assertIn("owner_floor_weekly", policy.GLOBAL_ONLY)
        with tempfile.TemporaryDirectory() as tmp:
            table = "".join(f"{k} = {v}\n" for k, v in policy.DEFAULTS.items() if k != "owner_floor_weekly")
            path = Path(tmp) / "policy.toml"
            path.write_text(table + "owner_floor_weekly = { value = 0, min = 1, max = 50 }\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "owner_floor_weekly must be a whole number between 1 and 50"):
                policy.read_table(path)


class Gate(Room):
    """alpha (share 30) is this project, beta (share 55) the other: the owner's slice is 15. Half the week is gone."""

    def setUp(self):
        super().setUp()
        owner_reserve._CACHE.clear()
        away = mock.patch.object(usage, "owner_active", return_value=None)   # this machine's sessions mustn't sway it
        away.start()
        self.addCleanup(away.stop)

    def registry(self, alpha=30, beta=55, budget=None):
        reg = Registry({"alpha": Project("alpha", self.project_dir("alpha", "alpha-site"), alpha),
                        "beta": Project("beta", self.project_dir("beta", "beta-site"), beta)}, dict(budget or {}))
        registry.save(reg)
        return reg

    def cfg(self, key="alpha"):
        return cfg_with(self.root / "work" / key)

    def five_hour(self, pct=5):
        for provider in ("claude", "codex"):
            self.ledger.record_usage([usage.observation(provider, "five_hour", pct, self.now + H, "test")],
                                     now=self.now - 60)

    def spend(self, alpha, beta, owner):
        """The week grows by `alpha` while alpha runs, then `beta` while beta runs, then `owner` with nothing running."""
        now = self.now
        self.add_run("alpha-site", now - 6 * H, now - 4 * H)
        self.add_run("beta-site", now - 4 * H, now - 2 * H)
        for at, pct in ((now - 6 * H, 0), (now - 4 * H, alpha), (now - 2 * H, alpha + beta),
                        (now - 60, alpha + beta + owner)):
            self.reading(at, pct)
        self.five_hour()

    def verdict(self, key="alpha", provider="claude"):
        return dispatch.verdicts(self.ledger, self.cfg(key), self.now)[provider]

    def test_projects_at_20_and_30_with_the_week_at_60_keep_back_5_and_go(self):
        self.registry()
        self.spend(20, 30, 10)
        report = standings.standings(self.ledger, registry.load(), "claude", self.now)
        self.assertEqual((round(report.projects_total, 6), round(report.owner_use, 6)), (50, 10))
        v = self.verdict()
        self.assertTrue(v.allowed, v.reason)
        self.assertEqual(v.detail["owner_reserve"], {"owner_share": 15, "owner_use": 10.0, "floor": 5,
                                                     "reserve_weekly": 5})

    def test_once_the_owner_has_used_his_slice_the_floor_is_kept_back(self):
        self.registry()
        self.spend(20, 30, 40)                                      # owner 40 of 15: his slice is used up
        v = self.verdict()
        self.assertTrue(v.allowed, v.reason)                        # 10 left of the week, 5 kept back
        self.assertEqual(v.detail["owner_reserve"]["reserve_weekly"], 5)
        owner_reserve._CACHE.clear()
        self.reading(self.now - 30, 95)                             # the owner goes on: 5 left
        v = self.verdict()
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "claude: weekly reserve reached (95% used, 5% kept back)")
        self.assertTrue(v.stop_running)                             # the owner's reserve stops running workers too

    def test_projects_at_their_whole_85_with_the_owner_at_5_stop(self):
        self.registry()
        self.spend(30, 55, 5)                                       # 10 of his slice unused, and 10 left of the week
        v = self.verdict()
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "claude: weekly reserve reached (90% used, 10% kept back)")
        self.assertEqual(v.detail["owner_reserve"]["reserve_weekly"], 10)
        self.assertEqual(v.reason, self.verdict("beta").reason)     # every registered project keeps back the same

    def test_the_owners_50_points_on_day_one_hold_only_an_unregistered_project_by_pace(self):
        self.registry()
        self.resets = self.now + 6 * 86400                          # one day of the week gone
        self.reading(self.now - 20 * H, 0)
        self.reading(self.now - 60, 50)                             # all of it the owner's own use
        self.five_hour()
        registered = self.verdict()
        self.assertTrue(registered.allowed, registered.reason)
        self.assertEqual(registered.detail["owner_reserve"]["reserve_weekly"], 5)
        unregistered = dispatch.verdicts(self.ledger, cfg_with(self.root), self.now)["claude"]
        self.assertFalse(unregistered.allowed)
        self.assertTrue(unregistered.reason.startswith("claude: ahead of weekly pace (50% used, pace allows 19%)"),
                        unregistered.reason)
        self.assertNotIn("owner_reserve", unregistered.detail)

    def test_the_registrys_5_hour_reserve_replaces_the_projects(self):
        self.registry(budget={"reserve_5h": 50})
        self.spend(20, 30, 10)
        self.five_hour(60)
        self.assertEqual(self.verdict().reason, "claude: 5-hour reserve reached (60% used, 50% kept back)")
        registry.save(Registry(registry.load().projects))           # no [budget]: the project's own 10
        self.assertNotIn("5-hour reserve", self.verdict().reason)

    def test_the_owners_slice_is_held_back_on_codex_too(self):
        self.registry()
        self.add_run("alpha-site", self.now - 6 * H, self.now - 4 * H, provider="codex")
        for at, pct in ((self.now - 6 * H, 0), (self.now - 4 * H, 20), (self.now - 60, 96)):
            self.reading(at, pct, provider="codex")
        self.five_hour()
        v = self.verdict(provider="codex")
        self.assertEqual(v.reason, "codex: weekly reserve reached (96% used, 5% kept back)")
        # Unregistered, codex may still be run dry (reserve 0) until the nearly-empty stop.
        self.assertEqual(dispatch.verdicts(self.ledger, cfg_with(self.root), self.now)["codex"].reason,
                         "codex: weekly window nearly empty (96% used)")

    def test_with_no_current_weekly_reading_the_gate_is_todays_with_a_note(self):
        self.registry()
        v = self.verdict()
        self.assertEqual(v.reason, "claude: no five_hour reading yet; the pie can't be read (it has no current weekly "
                                   "reading), so today's gate applies")

    def test_the_estimate_is_made_again_only_when_the_meter_moves(self):
        self.registry()
        self.spend(20, 30, 10)
        with mock.patch.object(standings, "standings", wraps=standings.standings) as made:
            def claude():
                return sum(1 for c in made.call_args_list if c.args[2] == "claude")

            for _ in range(3):
                self.verdict()
            self.assertEqual(claude(), 1)
            self.reading(self.now - 30, 61)
            self.verdict()
            self.assertEqual(claude(), 2)
            registry.save(Registry({k: p for k, p in registry.load().projects.items() if k == "alpha"}))
            self.verdict()
            self.assertEqual(claude(), 3)                           # a registry edit counts at once

    def test_with_the_plan_wide_pace_off_the_pie_holds_a_project_far_over_its_line_on_day_one(self):
        # The pie's gate (commonroom#240) is what paces a registered project once the plan-wide pace is off: no gap.
        self.registry()
        self.resets = self.now + 6 * 86400                          # one day of the week gone
        self.add_run("alpha-site", self.now - 20 * H, self.now - 2 * H)
        self.reading(self.now - 20 * H, 0)
        self.reading(self.now - 2 * H, 40)                          # all 40 points alpha's; its line is about 6.7
        self.reading(self.now - 60, 40)
        self.five_hour()
        d = dispatch.Dispatcher(self.cfg(), self.ledger, board=FakeBoard([]), log=lambda *_: None)
        d.claude_projects = self.transcripts
        gate_alone = self.verdict()
        self.assertTrue(gate_alone.allowed, gate_alone.reason)     # reserve 15 of the 60 left; no plan-wide pace
        unregistered = dispatch.verdicts(self.ledger, cfg_with(self.root), self.now)["claude"]
        self.assertIn("ahead of weekly pace (40% used", unregistered.reason)   # what held it before
        v = d.allowed(self.now)["claude"]
        self.assertFalse(v.allowed)
        self.assertTrue(v.reason.startswith("claude: the pie holds new work: alpha is over its line (40.0% spent of "
                                            "6.7%)"), v.reason)


class Guest(Room):
    """A guest (`[share] host`) in the registry takes no gate settings and no breakers from its host."""

    def setUp(self):
        super().setUp()
        owner_reserve._CACHE.clear()
        away = mock.patch.object(usage, "owner_active", return_value=None)
        away.start()
        self.addCleanup(away.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(config.HOST_ENV, None)
        host = self.root / "work" / "game"
        host.mkdir(parents=True)
        (host / "room.toml").write_text('[project]\nname = "game"\n[budget.claude]\nreserve_weekly = 65\nreserve_5h = 50\n',
                                        encoding="utf-8")
        self.guests = {}
        for key in ("alpha", "other"):
            folder = self.root / "work" / key
            folder.mkdir(parents=True)
            (folder / "room.toml").write_text(f'[project]\nname = "{key}-site"\n[share]\nhost = "{host.as_posix()}"\n',
                                              encoding="utf-8")
            self.guests[key] = config.load(folder / "room.toml")
        self.assertEqual(self.guests["alpha"]["_host"], "game")
        registry.save(Registry({"alpha": Project("alpha", str(self.root / "work" / "alpha"), 30)}))
        for provider in ("claude", "codex"):
            self.ledger.record_usage([usage.observation(provider, "five_hour", 20, self.now + H, "test"),
                                      usage.observation(provider, "seven_day", 0, self.resets, "test")],
                                     now=self.now - 3 * 86400)
            self.ledger.record_usage([usage.observation(provider, "five_hour", 20, self.now + H, "test"),
                                      usage.observation(provider, "seven_day", 40, self.resets, "test")],
                                     now=self.now - 60)

    def verdict(self, key):
        return dispatch.verdicts(self.ledger, self.guests[key], self.now)["claude"]

    def test_a_registered_guest_keeps_its_own_budget(self):
        self.assertEqual(self.verdict("other").reason, "claude: weekly reserve reached (40% used, 65% kept back)")
        v = self.verdict("alpha")
        self.assertTrue(v.allowed, v.reason)
        self.assertEqual(v.detail["owner_reserve"]["reserve_weekly"], 30)   # slice 70, all 40 points the owner's

    def test_a_registered_guest_is_not_stopped_by_its_hosts_breakers(self):
        self.ledger.record_usage([usage.observation("claude", "seven_day", 20, self.resets, "test")], now=self.now - 30)
        self.assertTrue(self.verdict("other").allowed, self.verdict("other").reason)   # under the host's budget
        self.ledger.trip("game", "paused_by_auditor", "owner pause")
        self.assertEqual(self.verdict("other").reason, "breaker open: paused_by_auditor - owner pause")
        self.assertTrue(self.verdict("alpha").allowed, self.verdict("alpha").reason)
        self.ledger.trip("alpha-site", "paused_by_auditor", "its own pause")
        self.assertIn("breaker open: paused_by_auditor - its own pause", self.verdict("alpha").reason)

    def test_config_keeps_a_guests_own_gate_settings_beside_the_hosts(self):
        cfg = self.guests["alpha"]
        self.assertEqual(cfg["budget"]["claude"]["reserve_weekly"], 65)                   # the host's, as today
        self.assertEqual(cfg["_own"]["budget"]["claude"]["reserve_weekly"], 25)           # its own: the default
        self.assertEqual(set(cfg["_own"]), set(config.GATE_KEYS))
        self.assertEqual(cfg["_own_budget"], {})                                          # its file sets none

    def test_the_5_hour_reserve_stays_todays_unless_the_registry_or_its_own_room_toml_sets_one(self):
        self.ledger.record_usage([usage.observation("claude", "five_hour", 60, self.now + H, "test"),
                                  usage.observation("claude", "seven_day", 20, self.resets, "test")], now=self.now - 30)
        held = "claude: 5-hour reserve reached (60% used, 50% kept back)"
        self.assertEqual(self.verdict("other").reason, held)
        self.assertEqual(self.verdict("alpha").reason, held)                 # the host's 50, inherited today, stays
        folder = self.root / "work" / "alpha"
        (folder / "room.toml").write_text((folder / "room.toml").read_text(encoding="utf-8")
                                          + "[budget.claude]\nreserve_5h = 20\n", encoding="utf-8")
        self.guests["alpha"] = config.load(folder / "room.toml")
        self.assertTrue(self.verdict("alpha").allowed, self.verdict("alpha").reason)   # its own room.toml's 20
        registry.save(Registry(registry.load().projects, {"reserve_5h": 70}))
        self.assertEqual(self.verdict("alpha").reason, "claude: 5-hour reserve reached (60% used, 70% kept back)")

    def test_when_the_standings_fail_a_registered_guest_gets_todays_gate_with_a_note(self):
        with mock.patch.object(standings, "standings", side_effect=RuntimeError("boom")):
            v, today = self.verdict("alpha"), self.verdict("other")
            self.assertEqual(v.reason, today.reason + "; the pie can't be read (its standings can't be computed: "
                                                      "boom), so today's gate applies")
            self.assertEqual(today.reason, "claude: weekly reserve reached (40% used, 65% kept back)")
            self.assertEqual((v.allowed, v.stop_running), (today.allowed, today.stop_running))
            self.assertIn("boom", v.detail["owner_reserve"]["unknown"])
            # Today's breakers too: the host's pause holds it again.
            self.ledger.record_usage([usage.observation("claude", "seven_day", 20, self.resets, "test")],
                                     now=self.now - 30)
            self.ledger.trip("game", "paused_by_auditor", "owner pause")
            self.assertEqual(self.verdict("alpha").reason, "breaker open: paused_by_auditor - owner pause")

    def test_a_registry_that_cant_be_read_gives_todays_gate_with_a_note(self):
        today = "claude: weekly reserve reached (40% used, 65% kept back)"
        alpha = (self.root / "work" / "alpha").as_posix()
        # A registry that lists alpha but breaks a rule: alpha gets today's gate with a note; other, unlisted, today's.
        (self.home / registry.FILENAME).write_text(f'[projects.alpha]\npath = "{alpha}"\nshare = 200\n', encoding="utf-8")
        v = self.verdict("alpha")
        self.assertEqual(v.reason, today + "; the pie can't be read (the project registry can't be read: project "
                                           "'alpha': 'share' must be a number from 0 to 100, not 200; shares sum to "
                                           "200, more than 100), so today's "
                                           "gate applies")
        self.assertTrue(v.stop_running)
        self.assertEqual(self.verdict("other").reason, today)
        # One that isn't TOML at all may list anyone: both get the note.
        (self.home / registry.FILENAME).write_text("[projects.alpha\n", encoding="utf-8")
        for key in ("alpha", "other"):
            v = self.verdict(key)
            self.assertTrue(v.reason.startswith(today + "; the pie can't be read (the project registry can't be read: "
                                                        "projects.toml is not valid TOML"), v.reason)
            self.assertTrue(v.reason.endswith("), so today's gate applies"), v.reason)


class Unchanged(Room):
    """With no registry, an empty one, or one that doesn't list the project, the gate is byte-for-byte today's."""

    def setUp(self):
        super().setUp()
        owner_reserve._CACHE.clear()
        away = mock.patch.object(usage, "owner_active", return_value=None)
        away.start()
        self.addCleanup(away.stop)

    def today(self, cfg):
        """The verdict as the code before commonroom#241 made it: the project's own [budget], untouched."""
        return {p: vars(gate.usage_verdict(p, self.ledger.latest_usage(p), config.budget(cfg, p), self.now,
                                           cfg["freshness_seconds"])) for p in ("claude", "codex")}

    def test_every_verdict_matches_todays(self):
        cfg = cfg_with(self.root)
        guest = cfg_with(self.root) | {"_host": "game"}
        cases = {"allowed": (5, 20), "pace": (5, 60), "reserve": (5, 80), "five hour": (95, 20), "stale": (5, 50)}
        files = {"no file": None, "empty": "", "others only": "listed"}
        reasons = {"allowed": "claude: allowed", "pace": "claude: ahead of weekly pace (60% used, pace allows 46%)",
                   "reserve": "claude: weekly reserve reached (80% used, 25% kept back)",
                   "five hour": "claude: 5-hour reserve reached (95% used, 10% kept back)",
                   "stale": "claude: seven_day reading is 40 min old and too close to its limit to trust"}
        for case, (five, week) in cases.items():
            with self.ledger.db:
                self.ledger.db.execute("DELETE FROM usage")             # each case its own readings
            for provider in ("claude", "codex"):
                at = self.now - (2400 if case == "stale" else 60)
                self.ledger.record_usage([usage.observation(provider, "five_hour", five, self.now + H, "test"),
                                          usage.observation(provider, "seven_day", week, self.resets, "test")], now=at)
            seen = []
            for name, text in files.items():
                path = self.home / registry.FILENAME
                if text is None:
                    path.unlink(missing_ok=True)
                elif text == "":
                    path.write_text("", encoding="utf-8")
                else:
                    other = self.root / "work" / f"x-{case}"
                    if not other.exists():
                        other.mkdir(parents=True)
                        (other / "room.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
                    registry.save(Registry({"x": Project("x", str(other), 50)}))
                with self.subTest(case=case, registry=name):
                    got = {p: vars(v) for p, v in dispatch.verdicts(self.ledger, cfg, self.now).items()}
                    self.assertEqual(got, self.today(cfg))
                    self.assertEqual(got["claude"]["reason"], reasons[case])
                    seen.append((got, {p: vars(v) for p, v in dispatch.verdicts(self.ledger, guest, self.now).items()}))
            self.assertTrue(all(s == seen[0] for s in seen), case)


if __name__ == "__main__":
    unittest.main()
