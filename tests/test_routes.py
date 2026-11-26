"""Routes (commonroom#576): where a session sent something, in the ledger and on the page, one can be undone."""
import json
import os
import unittest
from unittest import mock

from commonroom import page
from commonroom.ledger import Ledger
from tests.test_dispatch import cfg_with
from tests.test_handshake import Room


class Routes(Room):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": ""})
        env.start()
        self.addCleanup(env.stop)
        self.register()

    def rows(self):
        ledger = Ledger()
        try:
            return ledger.routes()
        finally:
            ledger.close()

    def test_a_route_is_recorded_and_listed_newest_first(self):
        code, out, err = self.room("route", "beta", "ask about the picks\nsecond line", "--how", "message",
                                   "--from", "alpha", "--session", "s1")
        self.assertEqual((code, err), (0, ""), err)
        self.room("route", "gamma", "second one", "--how", "brief", cwd=self.alpha)
        rows = self.rows()
        self.assertEqual([r["to_place"] for r in rows], ["gamma", "beta"])
        self.assertEqual((rows[1]["from_place"], rows[1]["session"], rows[1]["how"], rows[1]["summary"]),
                         ("alpha", "s1", "message", "ask about the picks second line"))
        self.assertEqual(rows[0]["from_place"], "alpha")          # taken from the folder it ran in
        code, out, _ = self.room("route", "list")
        self.assertEqual(code, 0)
        self.assertLess(out.index("gamma"), out.index("beta"))

    def test_undo_marks_the_route_and_keeps_it(self):
        self.room("route", "beta", "x", "--how", "note")
        rid = self.rows()[0]["id"]
        self.assertEqual(self.room("route", "undo", str(rid))[0], 0)
        self.assertIsNotNone(self.rows()[0]["undone_at"])
        self.assertIn("[undone", self.room("route", "list")[1])
        self.assertEqual(self.room("route", "undo", str(rid))[0], 1)       # already undone
        self.assertEqual(self.room("route", "undo", "999")[0], 1)

    def test_refuses_a_bad_how_or_empty_text(self):
        self.assertEqual(self.room("route", "beta", "x", "--how", "telepathy")[0], 1)
        self.assertEqual(self.room("route", "beta", "  ")[0], 1)
        self.assertEqual(self.rows(), [])

    def test_note_and_hand_across_places_record_a_route_and_within_a_place_do_not(self):
        self.room("note", "beta", "for beta", "--from", "desk", cwd=self.alpha)
        self.room("hand", "beta", "do it", "--to", "codex", cwd=self.alpha)
        self.room("note", "beta", "same place", cwd=self.beta)
        rows = self.rows()
        self.assertEqual([(r["from_place"], r["to_place"], r["how"]) for r in rows],
                         [("alpha", "beta", "hand"), ("alpha", "beta", "note")])

    def test_the_page_lists_routes_newest_first_with_undone_marked(self):
        self.room("route", "beta", "old", "--how", "note")
        self.room("route", "gamma", "new", "--how", "hand")
        self.room("route", "undo", str(self.rows()[0]["id"]))
        ledger = Ledger()
        try:
            d = page.page_state(ledger, cfg_with(self.tmp), 1.0e9)
        finally:
            ledger.close()
        json.dumps(d)
        self.assertEqual([r["summary"] for r in d["routes"]], ["new", "old"])
        self.assertIsNotNone(d["routes"][0]["undone_at"])


if __name__ == "__main__":
    unittest.main()
