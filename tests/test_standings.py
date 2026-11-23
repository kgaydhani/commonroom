"""Standings from the ledger and the registry, and `room pie` (commonroom#238). Every test uses a Ledger and a registry
in a temporary room home through COMMONROOM_HOME, never ~/.commonroom."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, dispatch, gate, liveness, pie, registry, sessions, standings, usage
from commonroom.ledger import Ledger
from commonroom.registry import Project, Registry, RegistryError
from tests.test_dispatch import FakeBoard, cfg_with

H = 3600


class Room(unittest.TestCase):
    """A week half gone at `now`. The meter reads 10 three hours ago, 16 two hours ago and 20 an hour ago: 6 points
    grew while alpha (cost $3 over its hour) and beta ($1 over its last half hour) ran, and 4 with nothing running.

    By hand, with the default Claude slack of 8:
      alpha: 6 x 3/(3+1) = 4.5 spent; line 40 x 0.5 + 8 x 0.40 = 23.2; headroom 18.7
      beta:  6 x 1/(3+1) = 1.5 spent; line 20 x 0.5 + 8 x 0.20 = 11.6; headroom 10.1
      projects 6.0; the owner's slice 40, his own use 20 - 6 = 14.0
    The registry keys (alpha, beta) differ from the names the runs carry (alpha-site, beta-site)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.ledger = Ledger(synchronous="OFF")
        self.addCleanup(self.ledger.close)
        self.transcripts = self.root / "claude-projects"          # never the real ~/.claude/projects
        self.transcripts.mkdir()
        transcripts = mock.patch.object(sessions, "default_dir", return_value=self.transcripts)
        transcripts.start()
        self.addCleanup(transcripts.stop)
        self.now = time.time()
        self.resets = self.now + 3.5 * 86400

    def project_dir(self, key: str, name: str | None) -> str:
        folder = self.root / "work" / key
        folder.mkdir(parents=True)
        (folder / "room.toml").write_text(f'[project]\nname = "{name}"\n' if name else "[project]\n", encoding="utf-8")
        return str(folder)

    def register(self, alpha=40, beta=20):
        reg = Registry({"alpha": Project("alpha", self.project_dir("alpha", "alpha-site"), alpha),
                        "beta": Project("beta", self.project_dir("beta", "beta-site"), beta, when="spare")})
        registry.save(reg)
        return registry.load()

    def reading(self, at, pct, resets=None, provider="claude"):
        self.ledger.record_usage([usage.observation(provider, "seven_day", pct, resets or self.resets, "test")], now=at)

    def add_run(self, project, start, end=None, cost=None, model="sonnet", provider="claude"):
        run = self.ledger.start_run(project, "#1", "builder", provider, model=model, now=start)
        if end is not None:
            self.ledger.finish_run(run, "done", cost_usd=cost, now=end)
        return run

    def week(self):
        now = self.now
        self.add_run("alpha-site", now - 3 * H, now - 2 * H, cost=3)
        self.add_run("beta-site", now - 2.5 * H, now - 2 * H, cost=1)
        for at, pct in ((now - 3 * H, 10), (now - 2 * H, 16), (now - H, 20)):
            self.reading(at, pct)

    def spent(self, report):
        return {s.name: round(s.spent, 6) for s in report.rows}


class Standings(Room):
    def test_standings_match_hand_worked_numbers(self):
        self.week()
        report = standings.standings(self.ledger, self.register(), "claude", self.now)
        self.assertAlmostEqual(report.window.elapsed, 0.5)
        self.assertEqual(report.window.used_pct, 20)
        self.assertEqual(self.spent(report), {"alpha": 4.5, "beta": 1.5})
        self.assertEqual([round(s.allowed, 6) for s in report.rows], [23.2, 11.6])
        self.assertEqual([round(s.headroom, 6) for s in report.rows], [18.7, 10.1])
        self.assertEqual([s.when for s in report.rows], ["paced", "spare"])
        self.assertAlmostEqual(report.projects_total, 6.0)
        self.assertEqual(report.owner_share, 40)
        self.assertAlmostEqual(report.owner_use, 14.0)
        self.assertEqual(report.unregistered, {})

    def test_a_registry_key_that_differs_from_the_project_name_still_gets_its_runs(self):
        self.week()
        reg = self.register()
        self.assertEqual(standings.ledger_names(reg), {"alpha": "alpha-site", "beta": "beta-site"})
        self.assertEqual(self.spent(standings.standings(self.ledger, reg, "claude", self.now))["alpha"], 4.5)

    def test_a_room_toml_with_no_name_maps_to_none_which_its_runs_carry(self):
        reg = Registry({"gamma": Project("gamma", self.project_dir("gamma", None), 10)})
        self.assertEqual(standings.ledger_names(reg), {"gamma": None})

    def test_a_null_project_run_is_counted_for_a_nameless_project(self):
        self.week()
        self.add_run(None, self.now - 3 * H, self.now - 2 * H)
        reg = Registry({"gamma": Project("gamma", self.project_dir("gamma", None), 10)})
        report = standings.standings(self.ledger, reg, "claude", self.now)
        self.assertGreater(report.rows[0].spent, 0)

    def test_a_null_project_run_with_no_registered_owner_never_reaches_a_sort(self):
        self.week()
        self.add_run(None, self.now - 3 * H, self.now - 2 * H)
        self.add_run("other", self.now - 2 * H, self.now - 1 * H)
        report = standings.standings(self.ledger, self.register(), "claude", self.now)
        self.assertIn(standings.NO_NAME, report.unregistered)
        self.assertTrue(all(isinstance(k, str) for k in report.unregistered))
        sorted(report.unregistered.items())

    def test_a_room_toml_with_an_unknown_approve_value_is_a_registry_error_naming_the_project(self):
        path = self.project_dir("zed", "zed-site")
        (Path(path) / "room.toml").write_text('[project]\nname = "zed-site"\n[goal]\napprove = "sometimes"\n', encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            standings.ledger_names(Registry({"zed": Project("zed", path, 10)}))
        self.assertIn("'zed'", str(ctx.exception))

    def test_two_keys_with_one_ledger_name_are_refused(self):
        reg = Registry({"a": Project("a", self.project_dir("a", "same"), 10),
                        "a2": Project("a2", self.project_dir("a2", "same"), 10)})
        with self.assertRaises(RegistryError):
            standings.ledger_names(reg)

    def test_a_reading_from_another_window_is_ignored(self):
        self.week()
        # A stale reader still reporting another window climbs 30 -> 50 while only alpha runs: not this window's growth.
        self.reading(self.now - 2.9 * H, 30, resets=self.resets + 2 * 86400)
        self.reading(self.now - 2.8 * H, 50, resets=self.resets + 2 * 86400)
        self.reading(self.now - 0.5 * H, 20)       # the latest reading is this window's again
        win = standings.window(self.ledger, "claude", self.now)
        self.assertEqual({s["resets_at"] for s in standings.samples(self.ledger, win, self.now)}, {self.resets})
        report = standings.standings(self.ledger, self.register(), "claude", self.now)
        self.assertEqual(self.spent(report), {"alpha": 4.5, "beta": 1.5})

    def test_an_open_run_with_a_dead_pid_is_ignored(self):
        self.week()
        open_run = self.add_run("beta-site", self.now - 1.9 * H)          # never finished: active up to now if alive
        with self.ledger.db:
            self.ledger.db.execute("UPDATE runs SET pid=? WHERE id=?", (os.getpid() + 1, open_run))
        reg = self.register()
        with mock.patch.object(liveness, "pid_alive", return_value=False):
            self.assertEqual(self.spent(standings.standings(self.ledger, reg, "claude", self.now)), {"alpha": 4.5, "beta": 1.5})
        with mock.patch.object(liveness, "pid_alive", return_value=True):
            # Alive, it counts: it alone ran in the 16 -> 20 gap.
            self.assertEqual(self.spent(standings.standings(self.ledger, reg, "claude", self.now)), {"alpha": 4.5, "beta": 5.5})

    def test_runs_with_no_model_and_other_providers_are_left_out(self):
        self.week()
        self.add_run("alpha-site", self.now - 1.5 * H, self.now - 1.2 * H, model=None)          # a model-less run
        self.add_run("alpha-site", self.now - 1.5 * H, self.now - 1.2 * H, provider="none", model=None)
        self.add_run("alpha-site", self.now - 1.5 * H, self.now - 1.2 * H, provider="codex", model="default")
        report = standings.standings(self.ledger, self.register(), "claude", self.now)
        self.assertEqual(self.spent(report), {"alpha": 4.5, "beta": 1.5})
        self.assertAlmostEqual(report.owner_use, 14.0)

    def test_an_unregistered_projects_spend_is_not_the_owners_own_use(self):
        self.week()
        self.add_run("other", self.now - 1.5 * H, self.now - 1.2 * H, cost=1)      # alone in the 16 -> 20 gap
        report = standings.standings(self.ledger, self.register(), "claude", self.now)
        self.assertEqual({k: round(v, 6) for k, v in report.unregistered.items()}, {"other": 4.0})
        self.assertAlmostEqual(report.projects_total, 6.0)
        self.assertAlmostEqual(report.owner_use, 10.0)

    def test_no_reading_gives_no_standings_and_no_error(self):
        reg = self.register()
        self.add_run("alpha-site", self.now - 3 * H, self.now - 2 * H, cost=3)
        self.assertIsNone(standings.standings(self.ledger, reg, "claude", self.now))
        self.assertIsNone(standings.standings(self.ledger, reg, "codex", self.now))

    def test_a_window_that_has_reset_gives_no_standings(self):
        self.reading(self.now - 2 * H, 30, resets=self.now - H)
        self.assertIsNone(standings.standings(self.ledger, self.register(), "claude", self.now))

    def test_now_in_the_past_sees_only_what_was_known_then(self):
        self.week()
        then = self.now - 1.5 * H                     # before the 20 reading
        report = standings.standings(self.ledger, self.register(), "claude", then)
        self.assertEqual(report.window.used_pct, 16)
        self.assertAlmostEqual(report.owner_use, 10.0)
        self.assertAlmostEqual(report.window.elapsed, pie.elapsed_fraction(then, self.resets))


class PieCommand(Room):
    def room(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["pie", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_empty_registry(self):
        code, out, _ = self.room()
        self.assertEqual(code, 0)
        self.assertIn("no projects are registered", out)
        code, out, _ = self.room("--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["providers"], {})

    def test_broken_registry(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "projects.toml").write_text('[projects.alpha]\npath = "nowhere"\nshare = 140\n', encoding="utf-8")
        code, out, err = self.room()
        self.assertEqual(code, 1)
        self.assertIn("'share' must be a number from 0 to 100", err)
        code, out, _ = self.room("--json")
        self.assertEqual(code, 1)
        self.assertIn("'share' must be a number from 0 to 100", json.loads(out)["error"])

    def test_two_projects(self):
        self.week()
        self.register()
        code, out, _ = self.room()
        self.assertEqual(code, 0, out)
        self.assertIn("est. = an estimate", out)
        self.assertIn("claude: 50% of the week gone", out)
        self.assertIn("plan-wide 20.0% used", out)
        self.assertIn("projects est. 6.0%; owner's slice 40, his own use est. 14.0%", out)
        self.assertRegex(out, r"alpha\s+40\s+paced\s+4\.5\s+4\.5\s+0\.0\s+23\.2\s+\+18\.7")
        self.assertRegex(out, r"beta\s+20\s+spare\s+1\.5\s+1\.5\s+0\.0\s+11\.6\s+\+10\.1")
        self.assertIn("STALE: the latest reading is from", out)          # an hour old
        self.assertIn("codex: no current weekly reading", out)
        code, out, _ = self.room("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIsNone(data["providers"]["codex"])
        claude = data["providers"]["claude"]
        self.assertTrue(claude["stale"])
        self.assertAlmostEqual(claude["owner_use_est"], 14.0)
        self.assertEqual([(p["name"], round(p["spent_est"], 3), round(p["line"], 3)) for p in claude["projects"]],
                         [("alpha", 4.5, 23.2), ("beta", 1.5, 11.6)])

    def test_a_fresh_reading_is_not_marked_stale(self):
        self.week()
        self.reading(time.time(), 20)                                   # seen again just now
        self.register()
        self.assertNotIn("STALE", self.room()[1])


class NoProjectsNoGate(Room):
    def test_dispatcher_allowed_is_the_same_with_no_registry_and_an_empty_one(self):
        # Since commonroom#240 a registry with projects gates (tests/test_pie_gate.py); with none, nothing changes.
        self.week()
        for provider in ("claude", "codex"):
            self.ledger.record_usage([usage.observation(provider, "five_hour", 5, self.now + H, "test"),
                                      usage.observation(provider, "seven_day", 20, self.resets, "test")], now=self.now)
        d = dispatch.Dispatcher(cfg_with(self.root), self.ledger, board=FakeBoard([]), log=lambda *_: None)

        def verdicts():
            return {p: (v.allowed, v.reason) for p, v in d.allowed(self.now).items()}

        without = verdicts()
        registry.save(Registry())                          # the file now exists, with no project in it
        self.assertTrue((self.home / registry.FILENAME).is_file())
        self.assertEqual(verdicts(), without)
        self.assertTrue(without["claude"][0], without)
        self.assertIsInstance(d.allowed(self.now)["claude"], gate.Verdict)


if __name__ == "__main__":
    unittest.main()
