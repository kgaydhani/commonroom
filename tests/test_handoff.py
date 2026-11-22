"""commonroom#363: `room handoff` writes a brief for the next session in the project's room folder, built part first,
and records its lineage in the ledger. All of it runs in a temporary room home, registry and ledger."""
import os
import subprocess
from pathlib import Path
from unittest import mock

from commonroom import handshake
from commonroom.ledger import Ledger
from tests.test_handshake import Room

TEXT = "I was halfway through the parser.\nNext: the tests for the empty case.\n"


class Handoff(Room):
    def setUp(self):
        super().setUp()
        ceiling = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)})
        ceiling.start()   # the temporary folder is never inside some other repository
        self.addCleanup(ceiling.stop)
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\n\n[goal]\nmilestone = "First release"\n',
                                              encoding="utf-8")
        self.register()

    def handoffs(self, name="alpha"):
        folder = self.folder(name) / "handoffs"
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def fields(self, path):
        return handshake._fields(path)

    def git_repo(self):
        def git(*argv):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv], cwd=self.alpha,
                           check=True, capture_output=True)
        git("init", "-q", "-b", "feature")
        git("add", "room.toml")
        git("commit", "-q", "-m", "first")
        (self.alpha / "new.txt").write_text("x", encoding="utf-8")
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.alpha, capture_output=True,
                              text=True, check=True).stdout.strip()

    def test_the_file_name_and_front_matter(self):
        commit = self.git_repo()
        code, out, err = self.room("handoff", "--session", "abc 123", "--reason", "context is large", "--context",
                                   "180000", cwd=self.alpha, stdin=TEXT)
        self.assertEqual((code, err), (0, ""))
        [path] = self.handoffs()
        self.assertRegex(path.name, r"^\d{8}-\d{6}-\d{6}-abc-123\.md$")
        self.assertIn(str(path), out)
        fields = self.fields(path)
        self.assertEqual({k: fields[k] for k in ("from_session", "reason", "context", "branch", "commit")},
                         {"from_session": "abc 123", "reason": "context is large", "context": "180000",
                          "branch": "feature", "commit": commit})
        self.assertRegex(fields["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertTrue(path.read_bytes().startswith(b"---\nfrom_session: abc 123\n"))
        self.assertNotIn(b"\r\n", path.read_bytes())

    def test_the_built_part_lists_open_work_and_the_sessions_text_follows_it(self):
        self.git_repo()
        ledger = Ledger()
        ledger.record_findings("alpha", [{"invariant": "blocked-on-nothing", "number": 7,
                                          "detail": "#7 is blocked on nothing"}])
        ledger.close()
        self.room("handoff", "alpha", "--session", "s1", "--reason", "switching", stdin=TEXT, cwd=self.alpha)
        text = self.handoffs()[0].read_text(encoding="utf-8")
        for part in ('Goal milestone: "First release".', "- #7 is blocked on nothing", "Branch `feature` at `",
                     "?? new.txt", "## From the session\n" + TEXT):
            self.assertIn(part, text)
        self.assertLess(text.index("#7 is blocked on nothing"), text.index("## From the session"))
        self.assertLess(text.index("?? new.txt"), text.index(TEXT))
        self.assertTrue(text.endswith(TEXT))

    def test_outside_a_git_repository_branch_and_commit_are_left_out(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        code, _, err = self.room("handoff", "alpha", "--session", "s1", "--reason", "done", cwd=elsewhere, stdin=TEXT)
        self.assertEqual((code, err), (0, ""))
        [path] = self.handoffs()
        self.assertNotIn("branch", self.fields(path))
        self.assertNotIn("commit", self.fields(path))
        self.assertNotIn("context", self.fields(path))
        self.assertIn("is not in a git repository.", path.read_text(encoding="utf-8"))

    def test_two_handoffs_from_one_session_in_one_instant_give_two_files(self):
        reg, p = handshake.load_registry(), None
        p = reg.projects["alpha"]
        ledger = Ledger()
        try:
            first = handshake.write_handoff(reg, p, ledger, "one", "s1", "r", cwd=self.tmp, now=1_800_000_000)
            second = handshake.write_handoff(reg, p, ledger, "two", "s1", "r", cwd=self.tmp, now=1_800_000_000)
        finally:
            ledger.close()
        self.assertNotEqual(first, second)
        self.assertEqual(self.handoffs(), sorted([first, second]))
        self.assertTrue(first.read_text(encoding="utf-8").endswith("one\n"))
        self.assertTrue(second.read_text(encoding="utf-8").endswith("two\n"))

    def test_the_lineage_mark_is_set(self):
        self.room("handoff", "alpha", "--session", "s9", "--reason", "r", stdin=TEXT)
        ledger = Ledger()
        try:
            marks = ledger.marks("alpha", "handoff:")
        finally:
            ledger.close()
        self.assertEqual([who for who, _ in marks], ["s9"])

    def test_the_digest_names_the_newest_handoff_and_nothing_else_changes(self):
        before = self.room("digest", "alpha")[1]
        beta = self.room("digest", "beta")[1]
        self.assertNotIn("## Newest handoff", before)
        self.room("handoff", "alpha", "--session", "old", "--reason", "r", stdin=TEXT)
        self.room("handoff", "alpha", "--session", "new", "--reason", "r", stdin=TEXT)
        digest = (self.folder("alpha") / "digest.md").read_text(encoding="utf-8")
        newest = self.handoffs()[-1]
        self.assertIn(f"## Newest handoff\n- `handoffs/{newest.name}`, ", digest)
        self.assertIn("from session new\n", digest)
        self.assertNotIn("from session old", digest)
        self.assertEqual(digest.split("\n## Newest handoff")[0].split("\n", 3)[3], before.split("\n", 3)[3])
        self.assertEqual(self.room("digest", "beta")[1].split("\n", 3)[3], beta.split("\n", 3)[3])
        self.assertFalse((self.folder("beta") / "handoffs").exists())

    def test_the_text_can_come_from_a_file(self):
        brief = self.tmp / "brief.md"
        brief.write_text("From a file.\n", encoding="utf-8")
        self.assertEqual(self.room("handoff", "alpha", "--session", "s", "--reason", "r", "--file", str(brief))[0], 0)
        self.assertTrue(self.handoffs()[0].read_text(encoding="utf-8").endswith("## From the session\nFrom a file.\n"))

    def test_each_refusal_writes_nothing(self):
        for argv, stdin in ((("--session", "s", "--reason", "r"), " \n\n"), (("--reason", "r"), TEXT),
                            (("--session", "s"), TEXT), (("--session", " ", "--reason", "r"), TEXT),
                            (("--session", "s", "--reason", ""), TEXT)):
            with self.subTest(argv=argv):
                code, out, err = self.room("handoff", "alpha", *argv, stdin=stdin)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("refused: "))
                self.assertEqual(self.handoffs(), [])
        ledger = Ledger()
        try:
            self.assertEqual(ledger.marks("alpha", "handoff:"), [])
        finally:
            ledger.close()
        self.assertFalse((self.folder("alpha") / "digest.md").exists())


class Take(Handoff):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("COMMONROOM_WORKER", None)   # these tests run as a person's session, not a room worker's

    def brief(self, session="old"):
        self.room("handoff", "alpha", "--session", session, "--reason", "context is large", "--context", "9000",
                  stdin=TEXT)
        return self.handoffs()[-1]

    def start(self, session="new1", cwd=None, **extra):
        from commonroom import hook
        return hook.context({"hook_event_name": "SessionStart", "session_id": session, "cwd": str(cwd or self.alpha),
                             **extra})

    def test_the_hook_names_an_untaken_brief_with_its_own_session_id(self):
        path = self.brief()
        text = self.start("sess-42")
        self.assertIn(str(path), text)
        self.assertIn("from session old", text)
        self.assertIn(f"room handoff take {path} --session sess-42", text)

    def test_a_taken_brief_is_not_offered_and_no_handoffs_changes_nothing(self):
        before = self.start()
        self.assertNotIn("Untaken handoff", before)
        path = self.brief()
        self.assertEqual(self.room("handoff", "take", str(path), "--session", "new1", cwd=self.alpha)[0], 0)
        self.assertEqual(self.start(), before)

    def test_take_records_old_to_new_and_refuses_a_second_take(self):
        path = self.brief()
        self.git_repo()
        code, out, err = self.room("handoff", "take", path.name, "--session", "new1", cwd=self.alpha)
        self.assertEqual((code, err), (0, ""))
        ledger = Ledger()
        try:
            [row] = ledger.lineage("alpha")
            self.assertIsNotNone(ledger.marked("alpha", "took:" + path.name))
        finally:
            ledger.close()
        self.assertEqual((row["old_session"], row["new_session"], row["reason"], row["context"], row["file"]),
                         ("old", "new1", "context is large", "9000", path.name))
        code, out, err = self.room("handoff", "take", path.name, "--session", "new2", cwd=self.alpha)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("already taken by session new1", err)
        ledger = Ledger()
        try:
            self.assertEqual([r["new_session"] for r in ledger.lineage("alpha")], ["new1"])
        finally:
            ledger.close()

    def test_take_refuses_what_is_not_a_brief_or_has_no_session(self):
        path = self.brief()
        for argv in (("take", "nope.md", "--session", "s"), ("take", path.name), ("take", str(self.alpha / "room.toml"), "--session", "s")):
            code, out, err = self.room("handoff", *argv, cwd=self.alpha)
            self.assertEqual((code, out), (1, ""))
            self.assertTrue(err.startswith("refused: "))

    def test_list_shows_taken_and_untaken_newest_first(self):
        self.assertIn("no handoffs", self.room("handoff", "list", "alpha")[1])
        first = self.brief("one")
        second = self.brief("two")
        self.room("handoff", "take", first.name, "--session", "three", cwd=self.alpha)
        lines = self.room("handoff", "list", "alpha")[1].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(f"from two to not taken: context is large (`{second.name}`)", lines[0])
        self.assertIn(f"from one to three: context is large (`{first.name}`)", lines[1])

    def test_a_room_worker_is_offered_nothing(self):
        self.brief()
        with mock.patch.dict(os.environ, {"COMMONROOM_WORKER": "1"}):
            self.assertEqual(self.start(), "")
