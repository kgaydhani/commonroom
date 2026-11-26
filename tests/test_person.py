"""The owner's person file (commonroom#573): one dated line per fact, capped in code, no secrets. Everything is built
in a temporary room home; nothing reads the owner's real one."""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from commonroom import cli, person, policy, steward

DAY = date(2026, 11, 24)


class PersonFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_limits_come_from_the_policy_table(self):
        self.assertEqual((person.MAX_LINES, person.MAX_TOKENS), (60, 800))
        self.assertEqual((policy.DEFAULTS["person_max_lines"], policy.DEFAULTS["person_max_tokens"]), (60, 800))

    def test_every_line_carries_its_date_and_the_file_is_lf(self):
        person.add("  Prefers   plain writing ", self.home, DAY)
        person.add("Works evenings", self.home, date(2026, 11, 25))
        self.assertEqual(person.load(self.home), ["2026-11-24 Prefers plain writing", "2026-11-25 Works evenings"])
        self.assertNotIn(b"\r", person.path(self.home).read_bytes())
        self.assertTrue(all(person.DATED.match(x) for x in person.load(self.home)))

    def test_today_is_used_when_no_date_is_given(self):
        person.add("a fact", self.home)
        self.assertTrue(person.load(self.home)[0].startswith(date.today().isoformat() + " "))

    def test_the_line_cap_refuses_and_lists_the_oldest_lines(self):
        for i in range(person.MAX_LINES):
            person.add(f"fact {i}", self.home, DAY)
        with self.assertRaises(person.PersonError) as caught:
            person.add("one too many", self.home, DAY)
        self.assertIn("2026-11-24 fact 0", str(caught.exception))
        self.assertIn("2026-11-24 fact 4", str(caught.exception))
        self.assertNotIn("fact 5", str(caught.exception))
        self.assertEqual(len(person.load(self.home)), person.MAX_LINES)
        person.remove(1, self.home)
        person.add("fits now", self.home, DAY)

    def test_the_token_cap_refuses(self):
        word = "word " * 30
        added = 0
        with self.assertRaisesRegex(person.PersonError, "about .* tokens of 800"):
            for _ in range(person.MAX_LINES):
                person.add(word, self.home, DAY)
                added += 1
        self.assertLess(added, person.MAX_LINES)
        self.assertLessEqual(person.tokens(person.load(self.home)), person.MAX_TOKENS)

    def test_tokens_are_characters_over_four_rounded_up(self):
        self.assertEqual(person.tokens(["abc"]), 1)      # 4 characters with the newline
        self.assertEqual(person.tokens(["abcd"]), 2)     # 5 characters
        self.assertEqual(person.tokens([]), 0)

    def test_secrets_are_refused_and_nothing_is_written(self):
        for text in ("my token is ghp_" + "a1B2c3D4e5" * 3, "key sk-abcdefghijklmnopqrstu", "password: hunter2hunter",
                     "AKIAABCDEFGHIJKLMNOP", "-----BEGIN RSA PRIVATE KEY-----", "Authorization: Bearer abcdefghijklmnop1234",
                     "api_key=abcd1234", "xoxb-1234567890-abcdef", "x" * 45):
            with self.subTest(text=text), self.assertRaisesRegex(person.PersonError, "looks like"):
                person.add(text, self.home, DAY)
        self.assertFalse(person.path(self.home).exists())

    def test_ordinary_lines_are_not_mistaken_for_secrets(self):
        for text in ("Lives in Leeds and uses a password manager", "Prefers short answers", "Token costs matter to him"):
            person.add(text, self.home, DAY)
        self.assertEqual(len(person.load(self.home)), 3)

    def test_empty_line_and_bad_remove_are_refused(self):
        with self.assertRaises(person.PersonError):
            person.add("   ", self.home, DAY)
        person.add("one", self.home, DAY)
        for n in (0, 2):
            with self.assertRaisesRegex(person.PersonError, "no line"):
                person.remove(n, self.home)

    def test_remove_drops_one_line(self):
        for t in ("a", "b", "c"):
            person.add(t, self.home, DAY)
        self.assertEqual(person.remove(2, self.home), "2026-11-24 b")
        self.assertEqual(person.load(self.home), ["2026-11-24 a", "2026-11-24 c"])


class RoomWide(unittest.TestCase):
    def test_a_project_cannot_set_the_caps_and_others_are_unchanged(self):
        for name in ("person_max_lines", "person_max_tokens"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "room-wide only"):
                policy.overrides({"policy": {name: 99}})
        self.assertEqual(policy.overrides({"policy": {"review_rounds": 3}}), {"review_rounds": 3})
        self.assertEqual(policy.overrides({}), {})


class Command(unittest.TestCase):
    def run_room(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["person", *argv])
        return code, out.getvalue()

    def test_show_add_and_remove_in_a_scratch_home(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"COMMONROOM_HOME": tmp}):
            self.assertEqual(self.run_room()[0], 0)
            self.assertEqual(self.run_room("add", "Likes tea")[0], 0)
            code, text = self.run_room()
            self.assertEqual(code, 0)
            self.assertIn(f"1. {date.today().isoformat()} Likes tea", text)
            self.assertIn("1 of 60 lines", text)
            code, text = self.run_room("add", "token = abcdef123456")
            self.assertEqual((code, "looks like" in text), (1, True))
            self.assertEqual(self.run_room("remove", "1")[0], 0)
            self.assertEqual(self.run_room("remove", "1")[0], 1)
            self.assertEqual(self.run_room("remove", "x")[0], 2)
            self.assertEqual(self.run_room("add")[0], 2)
            self.assertTrue((Path(tmp) / "person.md").exists())


if __name__ == "__main__":
    unittest.main()


class Stale(unittest.TestCase):
    """Old lines are flagged by the steward and never deleted (commonroom#574)."""
    def test_an_old_line_is_flagged_a_new_one_is_not_and_nothing_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            person.add("old fact", home, date(2026, 1, 1))
            person.add("new fact", home, date(2026, 11, 1))
            before = person.path(home).read_bytes()
            old = person.stale(person.load(home), DAY, policy.DEFAULTS["person_stale_days"])
            self.assertEqual(old, [(1, "2026-01-01 old fact")])
            found = steward.stale_person_lines(old, 180)
            self.assertEqual([(f["invariant"], f["number"]) for f in found], [("stale-person-line", 1)])
            self.assertIn("room person remove 1", found[0]["detail"])
            self.assertEqual(person.path(home).read_bytes(), before)

    def test_the_age_is_a_boundary_and_a_project_cannot_set_it(self):
        self.assertEqual(person.stale(["2026-05-28 x"], DAY, 180), [])
        self.assertEqual(len(person.stale(["2026-05-27 x"], DAY, 180)), 1)
        with self.assertRaisesRegex(ValueError, "room-wide only"):
            policy.overrides({"policy": {"person_stale_days": 9}})


class FromNote(unittest.TestCase):
    def make(self, root, kind, body="Keep replies short.\nmore"):
        inbox = Path(root) / "inbox"
        inbox.mkdir(exist_ok=True)
        note = inbox / "n1.md"
        note.write_text(f"---\nfrom: owner\nkind: {kind}\nat: 2026-11-20T10:00:00\n---\n{body}\n", encoding="utf-8")
        return note

    def test_a_correction_note_becomes_a_dated_line_through_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as proj, \
                mock.patch.dict(os.environ, {"COMMONROOM_HOME": tmp}):
            note = self.make(proj, "correction")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["person", "from-note", str(note)])
            self.assertEqual(code, 0)
            self.assertEqual(person.load(Path(tmp)), ["2026-11-20 Keep replies short."])

    def test_other_kinds_and_secrets_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as proj, \
                mock.patch.dict(os.environ, {"COMMONROOM_HOME": tmp}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["person", "from-note", str(self.make(proj, "decision"))]), 1)
                self.assertEqual(cli.main(["person", "from-note", str(self.make(proj, "correction", "token = abcdef123456"))]), 1)
                self.assertEqual(cli.main(["person", "from-note", str(Path(proj) / "inbox" / "gone.md")]), 1)
            self.assertEqual(person.load(Path(tmp)), [])
