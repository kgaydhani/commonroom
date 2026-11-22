"""commonroom#368: `room hand` gives work to another agent and the result comes back through the room folder."""
import re
import unittest
from unittest import mock

from commonroom import handshake
from commonroom.ledger import Ledger
from tests.test_handshake import Room


class Hand(Room):
    def give(self, brief="Review the parser change\nmore detail", to="codex"):
        code, out, err = self.room("hand", "--from", "desk", "--to", to, "alpha", brief)
        self.assertEqual(code, 0, err)
        return out.strip()

    def inbox(self):
        return sorted((self.folder("alpha") / "inbox").glob("*.md"))

    def mark(self, key):
        ledger = Ledger()
        try:
            return ledger.marked("alpha", key)
        finally:
            ledger.close()

    def digest(self):
        return (self.folder("alpha") / "digest.md").read_text(encoding="utf-8")

    def setUp(self):
        super().setUp()
        self.register()

    def test_hand_writes_front_matter_and_the_mark_and_prints_the_id(self):
        hid = self.give()
        self.assertRegex(hid, r"^[0-9a-f]{6}$")
        text = self.inbox()[0].read_text(encoding="utf-8")
        for line in ("kind: hand", "to: codex", f"hand: {hid}", "from: desk"):
            self.assertIn(line, text)
        self.assertIsNotNone(self.mark(f"hand:{hid}"))
        self.assertIsNone(self.mark(f"handed:{hid}"))

    def test_hand_needs_a_recipient_and_a_brief(self):
        self.assertEqual(self.room("hand", "alpha", "brief")[0], 1)
        self.assertEqual(self.room("hand", "--to", "x", "alpha", "  ")[0], 1)
        self.assertEqual(self.inbox(), [])

    def test_brief_from_standard_input(self):
        code, out, _ = self.room("hand", "--to", "codex", "alpha", "-", stdin="from stdin\n")
        self.assertEqual(code, 0)
        self.assertIn("from stdin", self.inbox()[0].read_text(encoding="utf-8"))

    def test_done_writes_the_result_and_closes_the_hand(self):
        hid = self.give()
        code, _, err = self.room("hand", "--project", "alpha", "--from", "codex", "done", hid, "Looks fine")
        self.assertEqual(code, 0, err)
        text = self.inbox()[-1].read_text(encoding="utf-8")
        self.assertIn("kind: result", text)
        self.assertIn(f"re: {hid}", text)
        self.assertIsNotNone(self.mark(f"handed:{hid}"))
        self.assertNotIn("Review the parser change", self.digest().split("## Handed, waiting")[1].split("Results:")[0])

    def test_unknown_id_and_second_done_are_refused_and_write_nothing(self):
        hid = self.give()
        code, _, err = self.room("hand", "--project", "alpha", "done", "ffffff", "x")
        self.assertEqual(code, 1)
        self.assertIn("no hand ffffff", err)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.room("hand", "--project", "alpha", "done", hid, "ok")[0], 0)
        code, _, err = self.room("hand", "--project", "alpha", "done", hid, "again")
        self.assertEqual(code, 1)
        self.assertIn("already done", err)
        self.assertEqual(len(self.inbox()), 2)

    def test_a_hand_written_re_note_closes_the_hand(self):
        hid = self.give()
        (self.folder("alpha") / "inbox" / "zz-answer.md").write_text(
            f"---\nfrom: partner\nre: {hid}\n---\n\nDone, merged.\n", encoding="utf-8")
        self.room("digest", "alpha")
        section = self.digest()
        self.assertNotIn("`%s`, to codex" % hid, section)
        self.assertIn(f"`{hid}`, partner: Done, merged.", section)
        code, _, err = self.room("hand", "--project", "alpha", "done", hid, "late")
        self.assertEqual(code, 1)
        self.assertIn("already done", err)

    def test_digest_lists_open_hands_and_five_newest_results(self):
        ids = [self.give(f"job {i}\nrest") for i in range(7)]
        for hid in ids[:6]:
            self.room("hand", "--project", "alpha", "done", hid, f"result of {hid}")
        digest = self.digest()
        self.assertIn("## Handed, waiting", digest)
        self.assertRegex(digest, rf"`{ids[6]}`, to codex, 0m ago: job 6\n")
        self.assertNotIn("rest", digest.split("## Handed, waiting")[1])
        self.assertEqual(len(re.findall(r"result of ", digest.split("Results:")[1])), 5)

    def test_digest_is_unchanged_with_no_hands(self):
        self.room("note", "alpha", "hello", "--from", "me")
        now = 1_800_000_000.0
        reg = handshake.load_registry()
        p = reg.projects["alpha"]
        ledger = Ledger()
        try:
            plain = handshake.build_digest(reg, p, ledger, now)
            with mock.patch.object(handshake, "_hands", return_value=""):
                self.assertEqual(handshake.build_digest(reg, p, ledger, now), plain)
        finally:
            ledger.close()
        self.assertNotIn("Handed", plain)


if __name__ == "__main__":
    unittest.main()
