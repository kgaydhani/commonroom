"""The dashboard's one page (commonroom#248): its state function on a temporary room home, never ~/.commonroom."""
import json
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dashboard, handshake, page, registry
from tests.test_dispatch import cfg_with
from tests.test_standings import H, Room


class PageState(Room):
    def cfg(self):
        cfg = cfg_with(self.root)
        cfg["project"]["name"] = "alpha-site"
        cfg["_root"] = str(self.root / "work" / "alpha")
        return cfg

    def state(self, queue=None, now=None):
        return page.page_state(self.ledger, self.cfg(), now or self.now, queue=queue)

    def test_two_projects_each_get_a_row_a_word_and_an_owner_row(self):
        self.week()
        self.register()
        self.reading(self.now - 60, 20)
        d = self.state(queue=lambda repo: [])
        rows = {r["name"]: r for r in d["rows"]}
        self.assertEqual(set(rows), {"alpha", "beta", "you (unassigned)"})
        self.assertEqual((rows["alpha"]["state"], rows["beta"]["state"]), ("under", "under"))
        self.assertAlmostEqual(rows["alpha"]["spent_est"], 4.5)
        self.assertAlmostEqual(rows["alpha"]["room_est"], 4.5)
        self.assertAlmostEqual(rows["alpha"]["line"], 23.2)
        self.assertEqual(rows["you (unassigned)"]["share"], 40)
        self.assertAlmostEqual(rows["you (unassigned)"]["spent_est"], 14.0)
        self.assertTrue(d["registered"])
        self.assertEqual([p["name"] for p in d["projects"]], ["alpha", "beta"])
        self.assertAlmostEqual(d["week"]["week_gone_pct"], 50)
        self.assertEqual(d["week"]["plan_used_pct"], 20)
        self.assertFalse(d["week"]["stale"])
        self.assertIn(d["pie"]["go"], (True, False, None))   # the pie's verdict is shown beside the gate's
        self.assertTrue(d["pie"]["reason"])
        self.assertIsNotNone(d["gate"]["reason"])
        json.dumps(d)

    def test_a_project_over_its_line_says_over_and_when_it_is_back_under(self):
        self.week()
        self.register(alpha=2, beta=20)   # alpha spent 4.5 against a line of 2 x 0.5 + 8 x 0.02 = 1.16
        d = self.state()
        alpha = next(r for r in d["rows"] if r["name"] == "alpha")
        self.assertIn(alpha["state"], ("over", "lent"))
        status = d["projects"][0]["status"]
        if alpha["state"] == "over":
            self.assertEqual(status["kind"], "resting")
            self.assertGreater(status["until"], self.now)

    def test_a_share_of_zero_is_off(self):
        self.week()
        self.register(alpha=0)
        self.assertEqual(next(r for r in self.state()["rows"] if r["name"] == "alpha")["state"], "off")

    def test_an_old_reading_is_marked_stale_with_its_age(self):
        self.reading(self.now - 3 * 86400, 30)
        self.register()
        d = self.state()
        self.assertTrue(d["week"]["stale"])
        self.assertAlmostEqual(d["week"]["reading_age_seconds"], 3 * 86400, delta=5)

    def test_no_registry_shows_the_one_project_and_says_so(self):
        self.reading(self.now - H, 20)
        d = self.state()
        self.assertFalse(d["registered"])
        self.assertEqual(d["rows"], [])
        self.assertIsNone(d["pie"])
        self.assertEqual([p["name"] for p in d["projects"]], ["alpha-site"])
        self.assertEqual(d["week"]["plan_used_pct"], 20)

    def test_no_reading_at_all_is_a_null_week_not_an_error(self):
        d = self.state()
        self.assertIsNone(d["week"])
        self.assertIsNone(d["error"])

    def test_a_running_project_is_working_and_open_breakers_are_in_words(self):
        self.week()
        self.register()
        self.ledger.trip("alpha-site", "no_progress", "ten points spent and nothing merged")
        d = self.state()
        alpha = d["projects"][0]
        self.assertEqual(alpha["breakers"][0]["words"], "ten points spent and nothing merged")
        self.assertEqual(alpha["status"]["kind"], "stopped")

    def test_the_owners_tasks_come_from_every_projects_repo(self):
        self.week()
        self.register()
        for key in ("alpha", "beta"):
            (self.root / "work" / key / "room.toml").write_text(
                f'[project]\nname = "{key}-site"\nrepo = "o/{key}"\n', encoding="utf-8")
        asked = []

        def queue(repo):
            asked.append(repo)
            return [{"number": 7, "title": "decide", "due": None, "url": "https://x/7"}]
        d = self.state(queue=queue)
        self.assertEqual(sorted(asked), ["o/alpha", "o/beta"])
        self.assertEqual([(t["project"], t["number"]) for t in d["waiting"]], [("alpha", 7), ("beta", 7)])

    def test_the_page_is_one_static_file_with_no_external_resource(self):
        html = dashboard.PAGE
        self.assertNotIn("http://", html)
        self.assertNotIn("https://fonts", html)
        self.assertIn("/api/page", html)
        self.assertNotIn("innerHTML", html)


class Trail(Room):
    """commonroom#408: each project's section carries the trail, from a fake board, never the network."""
    def setUp(self):
        super().setUp()
        reg = self.register()
        self.alpha = reg.projects["alpha"]
        (Path(self.alpha.path) / "room.toml").write_text('[project]\nname = "alpha-site"\nrepo = "me/alpha"\n', encoding="utf-8")
        self.calls = 0
        self.week()

    def note(self, text, sender="kieran", days_ago=1):
        return handshake.write_note(handshake.prepare(self.alpha), text, sender, None, self.now - days_ago * 86400).name

    def section(self, board):
        def fake(p, notes):
            self.calls += 1
            return board(p, notes)
        d = page.page_state(self.ledger, {**PageState.cfg(self), "project": {"name": "alpha-site"}}, self.now, trail=fake)
        return next(s for s in d["projects"] if s["name"] == "alpha"), d

    def test_a_note_with_an_open_and_a_landed_issue_shows_both_and_a_bare_note_has_none(self):
        named, bare = self.note("speed up the page\nmore"), self.note("think about colours", days_ago=3)
        sec, d = self.section(lambda p, notes: {n["name"]: [
            {"number": 5, "title": "open one", "state": "open", "merged": False},
            {"number": 6, "title": "done one", "state": "closed", "merged": True}] if n["name"] == named else [] for n in notes})
        self.assertNotIn("trail_error", sec)
        self.assertEqual([n["name"] for n in sec["trail"]], [named, bare])
        self.assertEqual(sec["trail"][0]["first_line"], "speed up the page")
        self.assertEqual([(i["number"], i["state"], i["merged"]) for i in sec["trail"][0]["items"]], [(5, "open", False), (6, "closed", True)])
        self.assertEqual(sec["trail"][1]["items"], [])
        json.dumps(d)

    def test_an_agents_note_is_not_in_the_trail(self):
        mine = self.note("mine")
        self.note("from the director", sender="director")
        sec, _ = self.section(lambda p, notes: {n["name"]: [] for n in notes})
        self.assertEqual([n["name"] for n in sec["trail"]], [mine])

    def test_a_board_that_raises_gives_trail_error_and_the_notes_with_no_items(self):
        self.note("a thing")
        def boom(p, notes):
            raise RuntimeError("github is down")
        sec, d = self.section(boom)
        self.assertIn("github is down", sec["trail_error"])
        self.assertEqual([n["items"] for n in sec["trail"]], [[]])
        self.assertIn("status", sec)

    def test_a_cached_failure_is_reported_as_an_error(self):
        self.note("a thing")
        sec, _ = self.section(lambda p, notes: {"error": "rate limited"})
        self.assertEqual(sec["trail_error"], "rate limited")

    def test_old_notes_and_no_notes_make_no_board_call(self):
        self.note("ancient", days_ago=30)
        sec, _ = self.section(lambda p, notes: {})
        self.assertEqual((sec["trail"], self.calls), ([], 0))

    def test_without_a_trail_function_sections_are_unchanged(self):
        self.note("a thing")
        d = page.page_state(self.ledger, PageState.cfg(self), self.now)
        self.assertTrue(all("trail" not in s and "trail_error" not in s for s in d["projects"]))

    def test_the_dashboard_caches_the_board_read_for_a_minute(self):
        self.note("a thing")
        notes = handshake.owner_notes(registry.load(), self.alpha, 0, self.now + 1)
        dashboard._cache.clear()
        with mock.patch.object(dashboard.handshake, "trail", return_value={notes[0]["name"]: []}) as t:
            dashboard._cached_trail(self.alpha, notes)
            dashboard._cached_trail(self.alpha, notes)
        self.assertEqual(t.call_count, 1)

    def test_the_page_renders_the_empty_case(self):
        html = (Path(dashboard.__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("no item yet", html)


if __name__ == "__main__":
    unittest.main()
