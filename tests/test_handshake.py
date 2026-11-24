"""commonroom#253: the handshake's first cut. Every registered project has one room folder any agent can be pointed
at, with `attach`, `note`, `notes` and `digest`. All of it runs in a temporary room home and registry."""
import contextlib
import io
import os
import subprocess
import tempfile
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from commonroom import board, cli, config as config_mod, handshake, registry
from commonroom.ledger import Ledger
from commonroom.localboard import LocalBoard

ROOT = Path(__file__).resolve().parent.parent
PARTS = ["README.md", "charter.md", "digest.md", "inbox"]


class Room(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        self.alpha, self.beta = self.project("alpha"), self.project("beta")

    def project(self, name):
        path = self.tmp / "work" / name
        (path / "src" / "deep").mkdir(parents=True)
        (path / "room.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
        return path

    def register(self, **shared):
        reg = registry.Registry()
        reg.projects["alpha"] = registry.Project("alpha", str(self.alpha), 30, shared=shared.get("alpha"))
        reg.projects["beta"] = registry.Project("beta", str(self.beta), 20)
        registry.save(reg)
        return reg

    def room(self, *argv, cwd=None, stdin=None):
        os.chdir(cwd or self.tmp)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin or "")):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def folder(self, name):
        return self.home / "projects" / name


class Attach(Room):
    def test_attach_from_the_projects_folder_makes_its_room_folder(self):
        self.register()
        code, out, _ = self.room("attach", "--as", "desk", cwd=self.alpha)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.folder("alpha").iterdir()), sorted(PARTS))
        self.assertTrue((self.folder("alpha") / "inbox").is_dir())
        self.assertIn(f"alpha: {self.folder('alpha')}", out)
        self.assertIn("Holds README.md, charter.md, digest.md, inbox/", out)
        self.assertIn((self.folder("alpha") / "digest.md").read_text(encoding="utf-8"), out)
        self.assertIn("- desk, last ", out)
        self.assertFalse(self.folder("beta").exists())

    def test_attach_from_a_subfolder_finds_the_project(self):
        self.register()
        code, out, _ = self.room("attach", cwd=self.alpha / "src" / "deep")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("alpha: "))

    def test_attach_by_name_from_anywhere(self):
        self.register()
        code, out, _ = self.room("attach", "beta", cwd=self.alpha)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("beta: "))
        self.assertEqual(sorted(p.name for p in self.folder("beta").iterdir()), sorted(PARTS))

    def test_attach_from_inside_the_room_folder_finds_its_project(self):
        self.register()
        self.room("attach", "beta")
        code, out, _ = self.room("attach", cwd=self.folder("beta") / "inbox")
        self.assertTrue(out.startswith("beta: "))

    def test_attach_from_an_unregistered_folder_says_so_and_names_projects_add(self):
        self.register()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        code, out, err = self.room("attach", cwd=elsewhere)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is in no registered project's folder (registered: alpha, beta)", err)
        self.assertIn("room projects add", err)
        self.assertFalse((self.home / "projects").exists())
        self.assertEqual(self.room("attach", "gamma")[2], "no project named 'gamma' is registered (registered: alpha, beta).\n")

    def test_a_hand_edited_charter_survives_attach_and_digest_byte_for_byte(self):
        self.register()
        self.room("attach", cwd=self.alpha)
        charter = self.folder("alpha") / "charter.md"
        self.assertIn("## The outcome, and why", charter.read_text(encoding="utf-8"))
        self.assertIn("Not written yet", (self.folder("alpha") / "digest.md").read_text(encoding="utf-8"))
        mine = "# Charter: alpha\r\n\r\n## The outcome, and why\r\nReaders who come back every week.\r\n\r\n## Open questions\r\nNot sure yet.\r\n"
        charter.write_bytes(mine.encode("utf-8"))
        self.room("attach", cwd=self.alpha)
        code, out, _ = self.room("digest", cwd=self.alpha)
        self.assertEqual(code, 0)
        self.assertEqual(charter.read_bytes(), mine.encode("utf-8"))
        self.assertIn("### The outcome, and why\nReaders who come back every week.", out)
        self.assertIn("Not sure yet.", out)
        self.assertEqual((self.folder("alpha") / "digest.md").read_text(encoding="utf-8"), out)

    def test_until_the_charter_is_written_the_digest_quotes_the_room_toml_mission_and_goal(self):
        self.register()
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\nmission = "Readers first."\n\n'
                                              '[goal]\nmilestone = "Issue one"\n', encoding="utf-8")
        out = self.room("digest", "--project", "alpha")[1]
        self.assertIn("Not written yet: `charter.md` holds only its headings.\nUntil it is, here is the mission from "
                      "the project's room.toml:\n\n> Readers first.\n\nIts current goal is the milestone \"Issue one\".", out)
        (self.folder("alpha") / "charter.md").write_text("# Charter\n\nThe owner's words.\n", encoding="utf-8")
        out = self.room("digest", "--project", "alpha")[1]
        self.assertIn("## What the owner wants\nThe owner's words.\n", out)
        self.assertNotIn("Readers first.", out)

    def test_the_readme_comes_from_its_template_and_says_what_the_files_are(self):
        self.register()
        self.room("attach", "alpha")
        readme = (self.folder("alpha") / "README.md").read_text(encoding="utf-8")
        template = (ROOT / "commonroom" / "ways" / "handshake" / "README.md").read_text(encoding="utf-8")
        self.assertEqual(readme, template.replace("{{project}}", "alpha"))
        for part in ("`charter.md`", "`digest.md`", "`inbox/`"):
            self.assertIn(part, readme)

    def test_the_templates_ship_with_the_package(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            self.assertIn("ways/handshake/*.md", tomllib.load(f)["tool"]["setuptools"]["package-data"]["commonroom"])


class OtherRoomToml(Room):
    def test_a_room_toml_with_an_unknown_approve_value_reads_as_empty(self):
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\n[goal]\napprove = "sometimes"\n', encoding="utf-8")
        self.assertEqual(handshake.room_toml(registry.Project("alpha", str(self.alpha), 30)), {})


class Notes(Room):
    def test_each_note_is_a_new_file_and_two_in_one_second_do_not_collide(self):
        self.register()
        where = handshake.prepare(self.register().projects["alpha"])
        now = datetime(2026, 11, 20, 14, 3, 12).timestamp()
        first = handshake.write_note(where, "first", "kieran", now=now)
        second = handshake.write_note(where, "second", "kieran", now=now)
        self.assertNotEqual(first, second)
        self.assertEqual([n["text"] for n in handshake.read_notes(where)], ["second", "first"])
        self.assertEqual(first.read_text(encoding="utf-8"), "---\nfrom: kieran\nat: 2026-11-20T14:03:12\n---\n\nfirst\n")

    def test_notes_lists_newest_first_with_hand_made_and_unreadable_files(self):
        self.register()
        self.assertEqual(self.room("note", "one", "--project", "alpha", "--from", "desk", "--to", "maria")[0], 0)
        inbox = self.folder("alpha") / "inbox"
        hand = inbox / "from-maria.txt"
        hand.write_text("Cover photo is in the drive.\n", encoding="utf-8")
        later = datetime.now().timestamp() + 60
        os.utime(hand, (later, later))
        (inbox / "scan.bin").write_bytes(b"\xff\xfe\x00\x81")
        os.utime(inbox / "scan.bin", (later - 3600, later - 3600))
        code, out, _ = self.room("notes", cwd=self.alpha)
        self.assertEqual(code, 0)
        heads = [line for line in out.splitlines() if line and not line.startswith(" ")]
        self.assertEqual(len(heads), 3, out)
        self.assertIn("(file time), `inbox/from-maria.txt`", heads[0])
        self.assertIn("  Cover photo is in the drive.", out)
        self.assertIn(", from desk to maria, `inbox/", heads[1])
        self.assertIn("`inbox/scan.bin`: not readable as text", heads[2])
        digest = (self.folder("alpha") / "digest.md").read_text(encoding="utf-8")
        self.assertIn("  > one", digest)

    def test_a_note_names_the_project_it_was_written_from_and_reads_standard_input(self):
        self.register()
        code, out, _ = self.room("note", "-", "--project", "beta", cwd=self.alpha / "src", stdin="from stdin\n")
        self.assertEqual(code, 0)
        [note] = handshake.read_notes(self.folder("beta"))
        self.assertEqual((note["from"], note["text"]), ("alpha", "from stdin"))
        self.assertEqual(self.room("note", "  ", "--project", "beta")[0], 1)

    def test_since_filters_notes(self):
        self.register()
        where = handshake.prepare(registry.load().projects["alpha"])
        handshake.write_note(where, "old", "a", now=datetime(2026, 1, 1).timestamp())
        handshake.write_note(where, "new", "a")
        code, out, _ = self.room("notes", "--project", "alpha", "--since", "2d")
        self.assertIn("new", out)
        self.assertNotIn("old", out)
        now = datetime(2026, 11, 20, 12).timestamp()
        self.assertEqual(handshake.parse_since("6h", now), now - 6 * 3600)
        self.assertEqual(handshake.parse_since("2026-11-20 14:00", now), datetime(2026, 11, 20, 14).timestamp())
        self.assertEqual(self.room("notes", "--project", "alpha", "--since", "soon")[0], 1)


class Digest(Room):
    def test_the_digest_holds_share_attachments_notes_and_what_ran_and_landed(self):
        self.register()
        now = datetime.now().timestamp()
        ledger = Ledger()
        for role, task, status, cost in (("builder", "#7", "done", 2.5), ("reviewer", "PR #9", "failed", 1.0)):
            run = ledger.start_run("alpha", task, role, "claude", now=now - 3600)
            ledger.finish_run(run, status, cost_usd=cost, now=now - 3000)
        merge = ledger.start_run("alpha", "#7", "merge", "none", now=now - 600)
        ledger.finish_run(merge, "done", outputs=[{"kind": "merge", "ref": "PR #9"}], now=now - 600)
        ledger.close()
        self.room("attach", "--as", "desk", cwd=self.alpha)
        code, out, _ = self.room("digest", "--project", "alpha")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# alpha\n\nBuilt by the room at "))
        self.assertIn("30% of the owner's weekly allowance (`open`: the planner may add work; `paced`:", out)
        self.assertIn("The owner's own share is 50%.", out)
        self.assertIn("- desk, last ", out)
        self.assertIn("2 runs (1 builder, 1 reviewer), 1 of them failed or blocked.", out)
        self.assertIn("About $4 of Claude use at API prices. That is an estimate", out)
        self.assertIn("Landed: 1.", out)
        self.assertIn(": #7 (PR #9)", out)
        self.assertNotIn("own count", out)   # no [progress] landed declared: nothing extra
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\n[progress]\nlanded = ["{python}", "-c", "print(1); print(2)"]\n', encoding="utf-8")
        code, out, _ = self.room("digest", "--project", "alpha")
        self.assertIn("Landed by the project's own count in 24 h: 2.", out)

    def landings(self, n, now):
        ledger = Ledger()
        for i in range(n):   # #1 is the oldest, #n the newest
            run = ledger.start_run("alpha", f"#{i + 1}", "merge", "none", now=now - 3600 * (n - i))
            ledger.finish_run(run, "done", outputs=[{"kind": "merge", "ref": f"PR #{100 + i + 1}"}], now=now - 3600 * (n - i))
        ledger.close()
        self.register()
        code, out, _ = self.room("digest", "--project", "alpha")
        self.assertEqual(code, 0)
        return out

    def test_the_digest_lists_the_10_newest_landings_and_counts_the_rest(self):
        out = self.landings(25, datetime.now().timestamp())
        self.assertIn("Landed: 25.", out)
        listed = [line for line in out.splitlines() if "(PR #" in line and line.startswith("- ")]
        self.assertEqual([line.split(": ", 1)[1] for line in listed],
                         [f"#{i} (PR #{100 + i})" for i in range(25, 15, -1)])
        older = [line for line in out.splitlines() if "older landings" in line]
        self.assertEqual(older, ["15 older landings in the last 7 days; the ledger has them."])

    def test_with_10_or_fewer_landings_every_one_is_listed_and_none_is_called_older(self):
        for n in (1, 9, 10):
            with self.subTest(n=n), mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.tmp / f"home{n}")}):
                out = self.landings(n, datetime.now().timestamp())
                self.assertIn(f"Landed: {n}.", out)
                listed = [line for line in out.splitlines() if "(PR #" in line and line.startswith("- ")]
                self.assertEqual(len(listed), n)
                self.assertNotIn("older landings", out)

    def test_board_constants_are_defined_once(self):
        source = Path(handshake.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("\nBOARD_CAP ="), 1)
        self.assertEqual(source.count("\nBOARD_FIRST ="), 1)

    def test_digest_with_hours_is_still_the_auditors_json(self):
        with mock.patch.object(cli, "digest", return_value={"window_hours": 3}) as old, \
                mock.patch.object(cli.config_mod, "load", return_value={}):
            code, out, _ = self.room("digest", "--hours", "3")
        self.assertEqual(code, 0)
        old.assert_called_once_with({}, 3.0)
        self.assertEqual(out, '{\n "window_hours": 3\n}\n')


class Reply(Room):
    """commonroom#260: the digest says how to reply, a worktree finds its project, README is never overwritten,
    every command takes the project as an argument or --project, and a note refreshes the digest."""

    def git(self, cwd, *argv):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=cwd, check=True,
                       capture_output=True)

    def test_the_digest_says_how_to_reply_and_where_the_readme_is(self):
        self.register()
        out = self.room("digest", "alpha")[1]
        self.assertIn('room note alpha "text"', out)
        self.assertIn(f"`inbox/` in {self.folder('alpha')}", out)
        self.assertIn("`README.md`", out)

    def test_attach_from_a_git_worktree_finds_the_registered_project(self):
        self.register()
        self.git(self.alpha, "init", "-q")
        self.git(self.alpha, "add", "-A")
        self.git(self.alpha, "commit", "-qm", "x")
        wt = self.tmp / "wt" / "alpha-branch"
        self.git(self.alpha, "worktree", "add", "-q", str(wt), "-b", "feature")
        code, out, err = self.room("attach", cwd=wt)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(f"alpha: {self.folder('alpha')}"))
        self.assertEqual(self.room("attach", cwd=self.tmp)[0], 1)   # still refused outside any project

    def test_a_hand_edited_readme_survives_attach_byte_for_byte(self):
        self.register()
        self.room("attach", cwd=self.alpha)
        readme = self.folder("alpha") / "README.md"
        mine = b"My own words.\r\nTwo lines.\r\n"
        readme.write_bytes(mine)
        code, out, _ = self.room("attach", cwd=self.alpha)
        self.assertEqual(code, 0)
        self.assertEqual(readme.read_bytes(), mine)
        self.assertIn("README.md was already there and is left as it is.", out)
        self.assertNotIn("left as it is", self.room("attach", "beta")[1])

    def test_every_command_takes_the_project_as_an_argument_or_with_project(self):
        self.register()
        for argv in (("attach", "alpha"), ("attach", "--project", "alpha"), ("digest", "alpha"),
                     ("digest", "--project", "alpha"), ("notes", "alpha"), ("notes", "--project", "alpha")):
            self.assertEqual(self.room(*argv)[0], 0, argv)
        self.assertEqual(self.room("note", "alpha", "one")[0], 0)
        self.assertEqual(self.room("note", "one", "--project", "alpha")[0], 0)
        self.assertEqual(self.room("note", "alpha", "two", "--project", "alpha")[0], 0)
        self.assertEqual(len(handshake.read_notes(self.folder("alpha"))), 3)
        self.assertEqual(self.room("digest", "alpha", "--project", "beta")[0], 1)
        self.assertEqual(self.room("note", "alpha", "beta", "text")[0], 1)

    def test_note_options_go_anywhere_among_the_words(self):
        self.register()
        for argv in (("alpha", "--from", "X", "--to", "Y", "text"), ("--from", "X", "alpha", "text"),
                     ("alpha", "text", "--from", "X", "--to", "Y"), ("alpha", "--from", "X", "two words")):
            self.assertEqual(self.room("note", *argv)[0], 0, argv)
        notes = handshake.read_notes(self.folder("alpha"))
        self.assertEqual(len(notes), 4)
        self.assertEqual({n["from"] for n in notes}, {"X"})
        self.assertEqual(sorted(n["to"] or "" for n in notes), ["", "", "Y", "Y"])
        self.assertEqual(self.room("note", "alpha", "--from", "X", "beta", "text")[0], 1)
        for argv in (("notes", "alpha", "--since", "6h"), ("attach", "alpha", "--as", "desk"),
                     ("digest", "--project", "alpha")):
            self.assertEqual(self.room(*argv)[0], 0, argv)

    def test_a_note_refreshes_the_projects_digest(self):
        self.register()
        self.room("attach", "alpha")
        digest = self.folder("alpha") / "digest.md"
        self.assertIn("None yet.", digest.read_text(encoding="utf-8"))
        self.room("note", "alpha", "please write the charter", "--from", "beta")
        self.assertIn("please write the charter", digest.read_text(encoding="utf-8"))


class Kinds(Room):
    def test_each_kind_is_written_as_front_matter(self):
        self.register()
        for kind in handshake.KINDS:
            self.assertEqual(self.room("note", "alpha", f"a {kind}", "--kind", kind)[0], 0)
        kinds = {n["kind"]: n["text"] for n in handshake.read_notes(self.folder("alpha"))}
        self.assertEqual(kinds, {k: f"a {k}" for k in handshake.KINDS})
        for path in (self.folder("alpha") / "inbox").iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertIn("kind: " + text.split("a ")[-1].split()[0] + chr(10), text)

    def test_unknown_kind_and_kind_with_direction_are_refused(self):
        self.register()
        code, _, err = self.room("note", "alpha", "x", "--kind", "hunch")
        self.assertNotEqual(code, 0)
        for kind in handshake.KINDS:
            self.assertIn(kind, err)
        self.assertNotEqual(self.room("note", "alpha", "x", "--kind", "decision", "--direction")[0], 0)
        self.assertEqual(handshake.read_notes(self.folder("alpha")), [])

    def test_notes_kind_lists_only_that_kind(self):
        self.register()
        self.room("note", "alpha", "chose A", "--kind", "decision")
        self.room("note", "alpha", "maybe B", "--kind", "suggestion")
        self.room("note", "alpha", "plain one")
        code, out, _ = self.room("notes", "alpha", "--kind", "decision")
        self.assertEqual(code, 0)
        self.assertIn("chose A", out)
        self.assertNotIn("maybe B", out)
        self.assertNotIn("plain one", out)
        self.assertIn("(decision)", out)

    def test_digest_shows_the_kind_beside_the_sender_only_when_there_is_one(self):
        self.register()
        self.room("attach", "alpha")
        self.room("note", "alpha", "chose A", "--from", "owner", "--kind", "decision")
        digest = (self.folder("alpha") / "digest.md").read_text(encoding="utf-8")
        self.assertIn("from owner (decision)", digest)
        self.room("note", "alpha", "plain", "--from", "owner2")
        digest = (self.folder("alpha") / "digest.md").read_text(encoding="utf-8")
        self.assertIn("from owner2,", digest)
        self.assertNotIn("owner2 (", digest)


class Decisions(Room):
    def digest(self):
        self.room("attach", "alpha")
        return (self.folder("alpha") / "digest.md").read_text(encoding="utf-8")

    def test_an_old_decision_stays_in_the_digest_past_the_newest_five(self):
        self.register()
        self.room("note", "alpha", "never use X\nbecause Y", "--from", "owner", "--kind", "decision")
        self.room("note", "alpha", "stay under 5 GB", "--from", "owner", "--kind", "bound")
        for i in range(6):
            self.room("note", "alpha", f"chatter {i}", "--from", "x")
        digest = self.digest()
        section = digest.split("## Decisions and bounds\n")[1].split("## Newest notes")[0]
        self.assertIn("owner (decision): never use X\n", section)
        self.assertNotIn("because Y", section)
        self.assertIn("owner (bound): stay under 5 GB", section)
        self.assertLess(digest.index("## What the owner wants"), digest.index("## Decisions and bounds"))
        self.assertNotIn("never use X", digest.split("## Newest notes")[1])

    def test_other_kinds_are_not_in_the_section(self):
        self.register()
        self.room("note", "alpha", "plain one", "--from", "o")
        self.room("note", "alpha", "maybe B", "--from", "o", "--kind", "suggestion")
        self.assertNotIn("## Decisions and bounds", self.digest())

    def test_twelve_decisions_give_ten_entries_and_a_left_out_line(self):
        self.register()
        for i in range(12):
            self.room("note", "alpha", f"decided {i}", "--from", "o", "--kind", "decision")
        section = self.digest().split("## Decisions and bounds\n")[1].split("## Newest notes")[0]
        self.assertEqual(section.count("(decision): decided"), 10)
        self.assertIn("decided 11", section)
        self.assertNotIn("decided 1\n", section)
        self.assertIn("- 2 more: `room notes alpha --kind decision`", section)

    def test_without_decisions_the_digest_has_no_section_and_is_unchanged(self):
        self.register()
        self.room("note", "alpha", "plain", "--from", "o")
        reg, p = handshake.load_registry(), handshake.load_registry().projects["alpha"]
        ledger = Ledger()
        try:
            now = datetime.now().timestamp()
            with mock.patch.object(handshake, "_decisions", return_value=""):
                before = handshake.build_digest(reg, p, ledger, now)
            self.assertEqual(handshake.build_digest(reg, p, ledger, now), before)
            self.assertNotIn("Decisions and bounds", before)
            self.assertIn("\n\n## Newest notes\n", before)
        finally:
            ledger.close()


class Direction(Room):
    def test_direction_note_reads_back_and_marks_the_ledger(self):
        self.register()
        before = datetime.now().timestamp()
        self.assertEqual(self.room("note", "alpha", "Stop X, do Y", "--direction")[0], 0)
        [note] = handshake.read_notes(self.folder("alpha"))
        self.assertEqual((note["kind"], note["text"]), ("direction", "Stop X, do Y"))
        ledger = Ledger()
        self.addCleanup(ledger.close)
        at = ledger.marked("alpha", "direction")
        self.assertGreaterEqual(at, before)
        self.assertEqual(int(at), int(note["at"]))

    def test_a_plain_note_records_no_direction(self):
        self.register()
        self.room("note", "alpha", "just a note")
        [note] = handshake.read_notes(self.folder("alpha"))
        self.assertIsNone(note["kind"])
        ledger = Ledger()
        self.addCleanup(ledger.close)
        self.assertIsNone(ledger.marked("alpha", "direction"))

    def test_direction_reads_standard_input(self):
        self.register()
        self.room("note", "-", "--project", "alpha", "--direction", stdin="from stdin\n")
        self.assertEqual(handshake.read_notes(self.folder("alpha"))[0]["kind"], "direction")

    def test_digest_shows_the_latest_direction_only_when_there_is_one(self):
        self.register()
        digest = self.folder("alpha") / "digest.md"
        self.room("attach", "alpha")
        self.assertNotIn("Latest direction", digest.read_text(encoding="utf-8"))
        self.room("note", "alpha", "old way", "--direction")
        self.room("note", "alpha", "Stop X,\n do Y " + "z" * 300, "--direction")
        text = digest.read_text(encoding="utf-8")
        [line] = [l for l in text.split("\n") if l.startswith("Latest direction: ")]
        self.assertIn("Stop X, do Y zzz", line)
        self.assertNotIn("old way", line)
        self.assertEqual(len(line.split(": ", 2)[2]), 200)


class SharedKey(Room):
    LIVE = registry.HEADER + ('\n[projects.alpha]\npath = "D:\\\\Dev\\\\alpha"\nshare = 55\nwork = "open"\nwhen = "paced"\n'
                              'strict = false\n')

    def test_a_registry_without_shared_reads_and_writes_exactly_as_before(self):
        path = self.home / "projects.toml"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.LIVE.encode("utf-8"))
        reg = registry.load(path, check=False)
        self.assertIsNone(reg.projects["alpha"].shared)
        registry.save(reg, path, changing="nobody")
        self.assertEqual(path.read_bytes(), self.LIVE.encode("utf-8"))

    def test_the_shared_key_moves_the_folder_and_round_trips(self):
        drive = self.tmp / "drive" / "alpha room"
        self.register(alpha=str(drive))
        text = registry.default_path().read_text(encoding="utf-8")
        self.assertEqual(tomllib.loads(text)["projects"]["alpha"]["shared"], str(drive))
        self.assertNotIn("shared", tomllib.loads(text)["projects"]["beta"])
        self.room("attach", cwd=self.alpha)
        self.assertEqual(sorted(p.name for p in drive.iterdir()), sorted(PARTS))
        self.assertFalse(self.folder("alpha").exists())
        code, out, _ = self.room("attach", cwd=drive)
        self.assertTrue(out.startswith("alpha: "))

    def test_shared_must_be_a_full_path(self):
        reg = registry.Registry()
        reg.projects["alpha"] = registry.Project("alpha", str(self.alpha), 30, shared="relative/room")
        self.assertIn("project 'alpha': 'shared' must be the full path of a folder, not 'relative/room'",
                      registry.problems(reg))


class Empty(Room):
    def test_with_no_projects_every_command_says_so_and_writes_nothing(self):
        self.home.mkdir()
        for argv in (["attach"], ["attach", "alpha"], ["note", "hello"], ["notes"], ["digest"],
                     ["digest", "--project", "alpha"]):
            with self.subTest(argv=argv):
                code, out, err = self.room(*argv, cwd=self.alpha)
                self.assertEqual(code, 1)
                self.assertIn("no projects are registered in", err)
                self.assertEqual(list(self.home.iterdir()), [])


class NoModel(Room):
    def test_no_command_reaches_a_model_or_github(self):
        self.register()
        def refuse(*args, **kwargs):
            raise AssertionError(f"reached outside the room: {args[:1]}")
        def only_git_rev_parse(argv, *args, **kwargs):   # finding a worktree's main repository is the one call allowed
            if list(argv[:2]) != ["git", "rev-parse"]:
                refuse(argv)
            return subprocess.CompletedProcess(argv, 128, "", "")   # as if not in a repo
        with mock.patch.object(subprocess, "run", only_git_rev_parse), mock.patch.object(subprocess, "Popen", refuse), \
                mock.patch.object(subprocess, "check_output", refuse), mock.patch.object(board, "gh", refuse), \
                mock.patch.object(board, "issues", refuse):
            for argv in (["attach"], ["note", "hi", "--project", "beta"], ["notes"], ["digest"]):
                with self.subTest(argv=argv):
                    self.assertEqual(self.room(*argv, cwd=self.alpha)[0], 0)


class CheckoutBehind(Room):
    """commonroom#524: the digest says when a project's checkout is behind its local origin/main."""

    def setUp(self):
        super().setUp()
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not installed")
        self.reg = self.register()
        self.origin = self.tmp / "origin.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.alpha, "init", "-q", "-b", "main")
        self.git(self.alpha, "add", "-A")
        self.git(self.alpha, "commit", "-qm", "first")
        self.git(self.alpha, "remote", "add", "origin", str(self.origin))
        self.git(self.alpha, "push", "-q", "origin", "main")
        self.other = self.tmp / "other"
        self.git(self.tmp, "clone", "-q", str(self.origin), str(self.other))

    def git(self, cwd, *argv):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=cwd, check=True,
                       capture_output=True)

    def push_two(self, room_toml=False):
        for i in range(2):
            (self.other / f"f{i}.txt").write_text("x", encoding="utf-8")
            if room_toml and i == 1:
                (self.other / "room.toml").write_text('[project]\nname = "alpha"\n[heartbeat]\ncommands = []\n', encoding="utf-8")
            self.git(self.other, "add", "-A")
            self.git(self.other, "commit", "-qm", f"c{i}")
        self.git(self.other, "push", "-q", "origin", "main")
        self.git(self.alpha, "fetch", "-q", "origin")

    def digest(self):
        ledger = Ledger()
        try:
            return handshake.build_digest(self.reg, self.reg.projects["alpha"], ledger)
        finally:
            ledger.close()

    def test_two_behind_with_room_toml_changed(self):
        self.push_two(room_toml=True)
        out = self.digest()
        self.assertIn("## Checkout behind main", out)
        self.assertIn("(branch main) is 2 commits behind origin/main, tree clean; room.toml differs", out)

    def test_two_behind_with_room_toml_unchanged(self):
        self.push_two()
        out = self.digest()
        self.assertIn("(branch main) is 2 commits behind origin/main, tree clean; room.toml is the same", out)

    def test_uncommitted_changes_are_named(self):
        self.push_two()
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\n# mine\n', encoding="utf-8")
        self.assertIn("uncommitted changes", self.digest())

    def test_up_to_date_ahead_no_origin_or_no_repo_has_no_section(self):
        self.assertNotIn("Checkout behind main", self.digest())
        (self.alpha / "g.txt").write_text("x", encoding="utf-8")
        self.git(self.alpha, "add", "-A")
        self.git(self.alpha, "commit", "-qm", "ahead")
        self.assertNotIn("Checkout behind main", self.digest())
        self.git(self.alpha, "remote", "remove", "origin")
        self.assertNotIn("Checkout behind main", self.digest())
        self.assertEqual(handshake._checkout_behind(registry.Project("beta", str(self.beta), 20)), "")   # not a repo

    def test_building_the_digest_runs_no_network_or_tree_changing_git(self):
        self.push_two()
        before = self.refs()
        real, seen = subprocess.run, []
        def record(argv, *a, **k):
            seen.append(list(argv))
            return real(argv, *a, **k)
        with mock.patch.object(subprocess, "run", record):
            self.digest()
        banned = {"fetch", "pull", "checkout", "reset", "merge", "clone", "push"}
        self.assertTrue(seen)
        self.assertFalse([a for a in seen if banned & set(a[1:])], seen)
        self.assertEqual(self.refs(), before)

    def refs(self):
        run = lambda *a: subprocess.run(["git", *a], cwd=self.alpha, capture_output=True, text=True).stdout
        return run("rev-parse", "HEAD"), run("for-each-ref"), run("status", "--porcelain")


class Corrections(Room):
    """commonroom#399: `--kind correction` notes, counted in the project digest and in the auditor's JSON summary."""
    NOW = datetime(2026, 11, 20, 12, 0).timestamp()
    DAY = 86400

    def note(self, days_ago, text):
        handshake.write_note(handshake.prepare(registry.load().projects["alpha"]), text, "kieran", None,
                             self.NOW - days_ago * self.DAY, "correction")

    def line(self):
        reg = registry.load()
        ledger = Ledger()
        try:
            text = handshake.build_digest(reg, reg.projects["alpha"], ledger, self.NOW)
        finally:
            ledger.close()
        return [l for l in text.split("\n") if l.startswith("Owner corrections")]

    def test_the_note_command_writes_a_correction_and_an_unknown_kind_is_still_refused(self):
        self.register()
        self.assertEqual(self.room("note", "alpha", "--kind", "correction", "use the shared one")[0], 0)
        self.assertEqual([n["kind"] for n in handshake.read_notes(self.folder("alpha"))], ["correction"])
        self.assertNotEqual(self.room("note", "alpha", "--kind", "hunch", "x")[0], 0)

    def test_three_this_week_and_one_before_give_one_line_with_the_newest_first_line(self):
        self.register()
        for days, text in ((6, "old one"), (3, "middle"), (1, "newest one\nsecond line")):
            self.note(days, text)
        self.note(10, "last week")
        self.assertEqual(self.line(), ["Owner corrections: 3 in the last 7 days, 1 in the 7 days before. Newest: newest one"])

    def test_none_in_fourteen_days_gives_no_line(self):
        self.register()
        self.assertEqual(self.line(), [])
        self.note(15, "too old")
        self.assertEqual(self.line(), [])

    def test_the_json_summary_carries_both_counts(self):
        self.register()
        for days in (1, 2, 3, 10):
            self.note(days, "x")
        cfg = config_mod.merge(config_mod.DEFAULTS, {"project": {"name": "alpha"}, "_path": None, "_root": None})
        with mock.patch("time.time", return_value=self.NOW):
            summary = cli.digest(cfg, 24)["summary"]
        self.assertEqual((summary["owner_corrections_7d"], summary["owner_corrections_prev_7d"]), (3, 1))


class WaitingOnYou(Room):
    """commonroom#409: the digest's `## Waiting on you` lists the project's open owner:human issues, overdue marked."""
    NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        super().setUp()
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\nrepo = "me/alpha"\n', encoding="utf-8")
        self.register()
        self.found, self.calls = [], []

    def digest(self, fake=None):
        def gh(args, timeout=30):
            self.calls.append((args, timeout))
            return self.found
        reg = registry.load()
        ledger = Ledger()
        try:
            with mock.patch.object(handshake.board, "gh", fake or gh):
                return handshake.build_digest(reg, reg.projects["alpha"], ledger, self.NOW)
        finally:
            ledger.close()

    def task(self, number, title="t", body="", created=None):
        self.found.append({"number": number, "title": title, "body": body, "createdAt": created or f"2026-11-{number:02d}T00:00:00Z"})

    def test_two_tasks_are_listed_oldest_first_and_the_past_due_one_is_overdue(self):
        self.task(9, "newer, no date")
        self.task(3, "older", "Due: 2026-11-19\nmore")
        text = self.digest()
        section = text.split("## Waiting on you\n")[1]
        self.assertEqual(section.strip().split("\n"), ["- #3 older, due 2026-11-19, overdue", "- #9 newer, no date"])
        args, timeout = self.calls[0]
        self.assertEqual((args[:2], "owner:human" in args, timeout), (["issue", "list"], True, handshake.TRAIL_TIMEOUT))
        self.assertEqual(len([c for c in self.calls if "open" in c[0]]), 1)

    def test_a_due_date_today_or_later_is_not_overdue(self):
        self.task(1, "today", "Due: 2026-11-20")
        self.assertNotIn("overdue", self.digest())

    def test_no_owner_tasks_no_section(self):
        self.assertNotIn("Waiting on you", self.digest())

    def test_an_unreadable_board_gives_one_line_and_the_digest_is_still_built(self):
        def boom(args, timeout=30):
            raise RuntimeError("down")
        text = self.digest(boom)
        self.assertIn("## Waiting on you\nthe board could not be read\n", text)
        self.assertIn("## Newest notes", text)

    def test_twelve_tasks_list_ten_and_two_more(self):
        for n in range(1, 13):
            self.task(n)
        lines = self.digest().split("## Waiting on you\n")[1].strip().split("\n")
        self.assertEqual((len(lines), lines[0], lines[-1]), (11, "- #1 t", "and 2 more"))

    def test_a_project_with_no_repo_makes_no_board_call(self):
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
        self.task(1)
        self.assertNotIn("Waiting on you", self.digest())
        self.assertEqual(self.calls, [])


class Interventions(Room):
    """commonroom#509: the digest sets the owner's interventions against landings, this week and the week before."""
    NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp()
    DAY = 86400

    def setUp(self):
        super().setUp()
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\nrepo = "me/alpha"\n', encoding="utf-8")
        self.register()
        self.items = []

    def note(self, days_ago):
        handshake.write_note(handshake.prepare(registry.load().projects["alpha"]), "x", "kieran", None,
                             self.NOW - days_ago * self.DAY, "correction")

    def item(self, days_ago):
        at = datetime.fromtimestamp(self.NOW - days_ago * self.DAY, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.items.append({"number": len(self.items) + 1, "createdAt": at})

    def land(self, n, days_ago=1):
        ledger = Ledger()
        try:
            for i in range(n):
                at = self.NOW - days_ago * self.DAY
                rid = ledger.start_run("alpha", f"t{i}", "merge", "claude", now=at)
                ledger.finish_run(rid, "done", now=at + 60)
        finally:
            ledger.close()

    def line(self, fail=False):
        def gh(args, timeout=30):
            if fail:
                raise RuntimeError("down")
            return self.items
        reg = registry.load()
        ledger = Ledger()
        try:
            with mock.patch.object(handshake.board, "gh", gh):
                text = handshake.build_digest(reg, reg.projects["alpha"], ledger, self.NOW)
                found = handshake.interventions(ledger, reg.projects["alpha"], self.NOW)
        finally:
            ledger.close()
        return [l for l in text.split("\n") if l.startswith("Owner interventions")][0], found

    def test_two_notes_and_one_item_against_four_landings(self):
        self.note(1); self.note(2); self.item(3); self.item(10); self.note(9)
        self.land(4)
        line, found = self.line()
        self.assertIn("3 in the last 7 days against 4 landings (0.75 per landing)", line)
        self.assertIn("the 7 days before: 2, no landings.", line)
        self.assertEqual((found["now"], found["before"], found["landings"]), (3, 2, 4))

    def test_no_landings_prints_counts_and_no_ratio(self):
        self.note(1)
        line, _ = self.line()
        self.assertIn("1 in the last 7 days, no landings", line)

    def test_an_unreadable_board_still_counts_notes_and_says_so(self):
        self.note(1); self.land(2)
        line, _ = self.line(fail=True)
        self.assertIn("1 in the last 7 days against 2 landings (0.5 per landing)", line)
        self.assertIn("board part is missing", line)

    def test_the_json_summary_carries_both_figures(self):
        self.note(1); self.note(10); self.item(2)
        cfg = config_mod.merge(config_mod.DEFAULTS, {"project": {"name": "alpha"}, "_path": None, "_root": None})
        with mock.patch("time.time", return_value=self.NOW), mock.patch.object(handshake.board, "gh", lambda a, timeout=30: self.items):
            summary = cli.digest(cfg, 24)["summary"]
        self.assertEqual((summary["owner_interventions_7d"], summary["owner_interventions_prev_7d"]), (2, 1))


class DirectorWakes(Interventions):
    """commonroom#503: director runs counted by wake reason, the 7 days before against the last 7 days."""

    def wake(self, days_ago, reason, role="director"):
        ledger = Ledger()
        try:
            at = self.NOW - days_ago * self.DAY
            rid = ledger.start_run("alpha", None, role, "claude", now=at, wake_reason=reason)
            ledger.finish_run(rid, "done", now=at + 60)
        finally:
            ledger.close()

    def lines(self):
        reg = registry.load()
        ledger = Ledger()
        try:
            with mock.patch.object(handshake.board, "gh", lambda a, timeout=30: []):
                text = handshake.build_digest(reg, reg.projects["alpha"], ledger, self.NOW)
        finally:
            ledger.close()
        return [l for l in text.split(chr(10)) if l.startswith("Director wakes")]

    def test_three_reasons_across_two_weeks(self):
        self.wake(1, "empty queue"); self.wake(2, "empty queue"); self.wake(10, "empty queue")
        self.wake(1, "review wanted: PR #5"); self.wake(3, "review wanted: PR #9")
        self.wake(9, "owner note"); self.wake(1, None); self.wake(12, None)
        self.wake(1, "empty queue", role="builder")
        self.assertEqual(self.lines(), [
            "Director wakes, empty queue: 1 in the 7 days before, 2 in the last 7 days.",
            "Director wakes, review wanted: 0 in the 7 days before, 2 in the last 7 days.",
            "Director wakes, unknown: 1 in the 7 days before, 1 in the last 7 days.",
            "Director wakes, owner note: 1 in the 7 days before, 0 in the last 7 days."])

    def test_no_director_runs_adds_no_line(self):
        self.wake(1, "x", role="builder")
        self.assertEqual(self.lines(), [])


class Trail(Room):
    """commonroom#400: `room trail` links each owner note to the issues filed from it; the digest counts notes with none."""
    NOW = datetime(2026, 11, 20, 12, 0).timestamp()
    DAY = 86400

    def setUp(self):
        super().setUp()
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\nrepo = "me/alpha"\n', encoding="utf-8")
        self.register()
        self.issues, self.prs = [], []
        patch = mock.patch.object(handshake.board, "gh", self.gh)
        patch.start()
        self.addCleanup(patch.stop)

    def gh(self, args, timeout=30):
        return self.issues if args[0] == "issue" else self.prs

    def note(self, days_ago, text, sender="kieran"):
        return handshake.write_note(handshake.prepare(registry.load().projects["alpha"]), text, sender, None,
                                    self.NOW - days_ago * self.DAY).name

    def issue(self, number, state, body):
        self.issues.append({"number": number, "title": f"t{number}", "state": state, "body": body})

    def trail(self):
        reg = registry.load()
        p = reg.projects["alpha"]
        notes = handshake.owner_notes(reg, p, self.NOW - 14 * self.DAY, self.NOW)
        return notes, handshake.trail(p, notes)

    def digest(self):
        reg = registry.load()
        ledger = Ledger()
        try:
            text = handshake.build_digest(reg, reg.projects["alpha"], ledger, self.NOW)
        finally:
            ledger.close()
        return [l for l in text.split("\n") if l.startswith("Owner notes with no item")]

    def test_a_note_named_by_two_issues_shows_both_with_their_state(self):
        name = self.note(2, "make the page faster")
        self.issue(5, "OPEN", f"Why\nFrom: {name}\n")
        self.issue(6, "CLOSED", f"From: {name}")
        self.prs = [{"number": 9, "closingIssuesReferences": [{"number": 6}]}]
        _, linked = self.trail()
        self.assertEqual([(i["number"], i["state"], i["merged"]) for i in linked[name]], [(5, "open", False), (6, "closed", True)])
        self.assertEqual(self.digest(), [])

    def test_the_command_prints_each_issue_and_a_note_with_none_says_no_item_yet(self):
        name = self.note(2, "make the page faster\nmore")
        self.note(1, "something else")
        self.issue(5, "OPEN", f"From: {name}")
        self.issue(6, "CLOSED", f"From: {name}")
        self.prs = [{"number": 9, "closingIssuesReferences": [{"number": 6}]}]
        with mock.patch("time.time", return_value=self.NOW):
            code, out, _ = self.room("trail", "alpha")
        self.assertEqual(code, 0)
        self.assertIn("#5 t5: open", out)
        self.assertIn("#6 t6: closed, merged PR", out)
        self.assertIn("something else", out)
        self.assertEqual(out.count("no item yet"), 1)
        self.assertLess(out.index("something else"), out.index("make the page faster"))

    def test_a_note_with_no_issue_is_counted_in_the_digest(self):
        self.note(2, "unanswered")
        self.assertEqual(self.digest(), ["Owner notes with no item yet: 1 in the last 14 days (`room trail alpha`)."])

    def test_from_in_the_middle_of_a_line_does_not_link(self):
        name = self.note(2, "x")
        self.issue(5, "OPEN", f"This came From: {name} I think")
        _, linked = self.trail()
        self.assertEqual(linked[name], [])
        self.assertEqual(len(self.digest()), 1)

    def test_an_agents_note_is_not_listed(self):
        self.note(2, "a project's", "beta")
        self.note(2, "a role's", "builder")
        mine = self.note(1, "the owner's")
        notes, _ = self.trail()
        self.assertEqual([n["name"] for n in notes], [mine])

    def test_no_notes_or_no_repo_means_no_board_call_and_no_line(self):
        def only_waiting(args, timeout=30):   # the one board call the digest makes for the owner's tasks (#409)
            if "owner:human" not in args:
                raise AssertionError("board read")
            return []
        self.gh = mock.Mock(side_effect=only_waiting)
        with mock.patch.object(handshake.board, "gh", self.gh):
            self.assertEqual(self.digest(), [])
            self.note(2, "x")
            (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
            self.assertEqual(self.digest(), [])

    def test_a_board_timeout_does_not_fail_the_note_command_or_add_a_line(self):
        self.note(2, "x")
        boom = mock.Mock(side_effect=subprocess.TimeoutExpired("gh", 10))
        with mock.patch.object(handshake.board, "gh", boom):
            code, _, _ = self.room("note", "alpha", "hello")
            self.assertEqual(code, 0)
            self.assertEqual(self.digest(), [])
            self.assertNotEqual(self.room("trail", "alpha")[0], 0)


class LocalBoardDigest(Room):
    """commonroom#476: on a `board = "local"` project the digest's owner tasks and `room trail` read the local board."""
    NOW = datetime(2026, 11, 20, 12, 0).timestamp()

    def setUp(self):
        super().setUp()
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\nboard = "local"\n', encoding="utf-8")
        self.reg = self.register()
        self.p = self.reg.projects["alpha"]
        self.board = LocalBoard(handshake.folder_of(self.p))
        patch = mock.patch.object(handshake.board, "gh", mock.Mock(side_effect=AssertionError("gh called")))
        patch.start()
        self.addCleanup(patch.stop)

    def digest(self):
        ledger = Ledger()
        try:
            return handshake.build_digest(self.reg, self.p, ledger, self.NOW)
        finally:
            ledger.close()

    def test_an_open_owner_task_with_a_due_line_is_waiting_on_you(self):
        self.board.create_issue("sign the form", "Due: 2026-11-19\n", labels=["owner:human"])
        self.board.create_issue("not his", "x", labels=["status:ready"])
        done = self.board.create_issue("done one", "x", labels=["owner:human"])
        self.board.close_issue(done, "done")
        section = self.digest().split("## Waiting on you\n")[1].split("\n\n")[0]
        self.assertEqual(section.strip().split("\n"), ["- #1 sign the form, due 2026-11-19, overdue"])

    def test_a_note_links_to_the_item_filed_from_it(self):
        name = handshake.write_note(handshake.prepare(self.p), "do the thing", "kieran", None, self.NOW - 86400).name
        self.assertEqual(handshake.unlinked(self.reg, self.p, self.NOW), 1)
        number = self.board.create_issue("the thing", f"Why\nFrom: {name}\n")
        self.assertEqual(handshake.unlinked(self.reg, self.p, self.NOW), 0)
        notes = handshake.owner_notes(self.reg, self.p, self.NOW - 14 * 86400, self.NOW)
        self.assertEqual([(i["number"], i["state"], i["merged"]) for i in handshake.trail(self.p, notes)[name]], [(number, "open", False)])
        self.board.close_issue(number, "done")
        self.assertEqual([(i["state"], i["merged"]) for i in handshake.trail(self.p, notes)[name]], [("closed", False)])


if __name__ == "__main__":
    unittest.main()
