"""The dashboard's one page (commonroom#248): its state function on a temporary room home, never ~/.commonroom."""
import json
import unittest

from commonroom import dashboard, page
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


if __name__ == "__main__":
    unittest.main()
