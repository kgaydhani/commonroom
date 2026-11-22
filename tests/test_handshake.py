"""commonroom#253: the handshake's first cut. Every registered project has one room folder any agent can be pointed
at, with `attach`, `note`, `notes` and `digest`. All of it runs in a temporary room home and registry."""
import contextlib
import io
import os
import subprocess
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from commonroom import board, cli, handshake, registry
from commonroom.ledger import Ledger

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


if __name__ == "__main__":
    unittest.main()
