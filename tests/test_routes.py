"""Routes (commonroom#576): where a session sent something, in the ledger and on the page, one can be undone."""
import json
import os
import unittest
from unittest import mock

from commonroom import hook, page, steward
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


class Questions(Routes):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("COMMONROOM_WORKER", None)   # a room worker runs these tests: the hook must not think it is one

    def ask(self, session="s1", within=None):
        extra = ["--within", str(within)] if within else []
        code, out, err = self.room("route", "beta", "which pick is live?", "--ask", "--from", "alpha", "--session", session, *extra)
        self.assertEqual((code, err), (0, ""), err)
        return self.rows()[0]["id"]

    def prompt(self, session="s1"):
        return hook.answers_back({"session_id": session, "cwd": str(self.alpha)})

    def test_asking_records_who_what_and_by_when(self):
        before = __import__("time").time()
        rid = self.ask(within=2)
        row = self.rows()[0]
        self.assertEqual((row["session"], row["from_place"], row["to_place"], row["summary"]),
                         ("s1", "alpha", "beta", "which pick is live?"))
        self.assertAlmostEqual(row["due"], before + 7200, delta=60)
        self.assertIn("[question, answer due", self.room("route", "list")[1])
        self.assertIsNone(self.room("route", "beta", "plain", "--session", "s1") and self.rows()[0]["due"])  # not a question

    def test_an_answer_closes_it_and_reaches_the_asking_session_once(self):
        rid = self.ask()
        self.assertEqual(self.prompt(), "")                                 # nothing yet
        code, _, err = self.room("route", "answer", str(rid), "the second one")
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(self.prompt("other"), "")                          # another session gets nothing
        text = self.prompt()
        self.assertIn("the second one", text)
        self.assertIn("beta", text)
        self.assertEqual(self.prompt(), "")                                 # delivered once
        self.assertEqual(self.room("route", "answer", str(rid), "again")[0], 1)
        self.assertIn("[answered", self.room("route", "list")[1])

    def test_only_a_question_can_be_answered_and_an_undone_one_is_not_delivered(self):
        self.room("route", "beta", "plain", "--session", "s1")
        self.assertEqual(self.room("route", "answer", str(self.rows()[0]["id"]), "x")[0], 1)
        rid = self.ask()
        self.room("route", "answer", str(rid), "late")
        self.room("route", "undo", str(rid))
        self.assertEqual(self.prompt(), "")

    def test_the_prompt_hook_carries_the_answer(self):
        rid = self.ask()
        self.room("route", "answer", str(rid), "yes")
        out = __import__("io").StringIO()
        hook.main(__import__("io").StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                                        "cwd": str(self.alpha)})), out, event="UserPromptSubmit")
        self.assertIn("yes", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])

    def test_an_unanswered_question_past_its_time_is_a_finding(self):
        rid = self.ask()
        answered = self.ask()
        self.room("route", "answer", str(answered), "done")
        ledger = Ledger()
        try:
            now = self.rows()[0]["due"] + 3 * 3600
            over = ledger.overdue_questions(now)
            self.assertEqual(ledger.overdue_questions(now - 5 * 3600), [])
        finally:
            ledger.close()
        found = steward.overdue_questions(over, now)
        self.assertEqual([(f["invariant"], f["number"]) for f in found], [("unanswered-question", rid)])
        self.assertIn(f"room route undo {rid}", found[0]["detail"])


if __name__ == "__main__":
    unittest.main()
