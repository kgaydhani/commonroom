"""The page's layer section (commonroom#585): built from a scratch room home and a temporary ledger."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from commonroom import glance, page, person
from commonroom.ledger import Ledger
from tests.test_dispatch import cfg_with

NOW = datetime(2026, 11, 27, 15, 0).timestamp()


class Glance(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.ledger = Ledger(synchronous="OFF")
        self.addCleanup(self.ledger.close)

    def test_routes_today_by_place_with_undone_counted_apart_and_yesterday_left_out(self):
        for to, how, at in (("beta", "note", NOW - 60), ("beta", "note", NOW - 30), ("gamma", "hand", NOW - 20),
                            ("beta", "note", NOW - 86400 * 2)):
            self.ledger.add_route("s", "alpha", to, "x", how, at=at)
        undone = self.ledger.add_route("s", "alpha", "gamma", "y", "hand", at=NOW - 10)
        self.ledger.undo_route(undone)
        r = glance.layer(self.ledger, NOW)["routes"]
        self.assertEqual((r["total"], r["undone"]), (3, 1))
        self.assertEqual(r["to"], [{"place": "beta", "how": "note", "count": 2}, {"place": "gamma", "how": "hand", "count": 1}])
        self.assertTrue(r["how"] and r["leaves_out"])

    def test_offers_today_and_took_is_not_recorded(self):
        (self.home / glance.OFFERS).write_text(json.dumps({"a": {"at": NOW - 5}, "b": {"at": NOW - 86400 * 3}, "c": "bad"}))
        o = glance.layer(self.ledger, NOW)["offers"]
        self.assertEqual((o["offered"], o["took"]), (1, None))
        self.assertIn("not recorded", o["leaves_out"])

    def test_no_offer_file_is_zero_and_a_corrupt_one_is_said(self):
        self.assertEqual(glance.layer(self.ledger, NOW)["offers"]["offered"], 0)
        (self.home / glance.OFFERS).write_text("{nope")
        o = glance.layer(self.ledger, NOW)["offers"]
        self.assertIsNone(o["offered"])
        self.assertIn("error", o)

    def test_person_file_size_against_cap_and_oldest_line(self):
        person.save(["2026-11-20 likes plain writing", "2026-11-25 works nights"])
        p = glance.layer(self.ledger, NOW)["person"]
        self.assertEqual((p["lines"], p["max_lines"], p["max_tokens"]), (2, person.MAX_LINES, person.MAX_TOKENS))
        self.assertEqual((p["oldest"], p["oldest_days"]), ("2026-11-20", 7))
        self.assertEqual(p["tokens"], person.tokens(person.load()))

    def test_empty_person_file(self):
        p = glance.layer(self.ledger, NOW)["person"]
        self.assertEqual((p["lines"], p["oldest"]), (0, None))

    def test_the_page_state_carries_the_section_and_stays_json(self):
        self.ledger.add_route("s", "alpha", "beta", "x", "note", at=time.time())
        d = page.page_state(self.ledger, cfg_with(self.home), time.time())
        json.dumps(d)
        self.assertEqual(d["layer"]["routes"]["total"], 1)
        self.assertEqual(set(d["layer"]), {"since", "routes", "offers", "person"})


if __name__ == "__main__":
    unittest.main()
