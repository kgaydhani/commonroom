"""commonroom#262: the SessionStart hook that points a session at its project's room folder. All of it runs in a
temporary room home and registry."""
import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import board, cli, handshake, hook, registry
from commonroom.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "commonroom" / "ways" / "handshake" / "session-start.md"
GIT = shutil.which("git")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


class Hook(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("COMMONROOM_WORKER", None)
        self.alpha = self.tmp / "work" / "alpha"
        (self.alpha / "src").mkdir(parents=True)
        (self.alpha / "room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
        self.elsewhere = self.tmp / "elsewhere"
        self.elsewhere.mkdir()

    def register(self):
        reg = registry.Registry()
        reg.projects["alpha"] = registry.Project("alpha", str(self.alpha), 30)
        registry.save(reg)
        return reg

    def room(self, argv, cwd):
        """Run `room <argv>` from `cwd`, printing nothing; returns the exit code."""
        before = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, before)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(list(argv))

    def note(self, text, sender, at=None):
        p = handshake.find(registry.load(), "alpha")
        return handshake.write_note(handshake.prepare(p), text, sender, now=at)

    def run_hook(self, stdin, limit=hook.LIMIT):
        """`room hook session-start` with `stdin`: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch.object(hook, "LIMIT", limit):
            code = cli.main(["hook", "session-start"])
        return code, out.getvalue(), err.getvalue()

    def added(self, cwd, **extra):
        """The context the hook adds for a session starting in `cwd`, or "" when it prints nothing."""
        data = {"session_id": "s1", "cwd": str(cwd), "hook_event_name": "SessionStart", "source": "startup", **extra}
        code, out, err = self.run_hook(json.dumps(data))
        self.assertEqual((code, err), (0, ""))
        if not out:
            return ""
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def folder(self):
        return self.home / "projects" / "alpha"


class Points(Hook):
    def test_a_registered_folder_gets_the_digest_path_the_unread_notes_and_how_to_reply(self):
        self.register()
        self.note("Please write your charter.", "director")
        self.note("Second thought.", "director")
        self.note("Hello from beta.", "beta")
        text = self.added(self.alpha / "src")
        self.assertIn(f"`{self.folder() / 'digest.md'}`", text)
        self.assertIn("Unread notes in its `inbox/`: 3, from director (2), beta.", text)
        self.assertIn('`room note alpha "text"`', text)
        for words in ("write your charter", "Second thought", "Hello"):   # senders and a count, never a note's text
            self.assertNotIn(words, text)
        self.assertEqual(text, handshake.fill_way(hook.SESSION_START, project="alpha",
                                                  digest=str(self.folder() / "digest.md"),
                                                  notes="3, from director (2), beta",
                                                  reply=handshake.reply_line(registry.load().projects["alpha"])))

    @unittest.skipUnless(GIT, "git is not installed")
    def test_a_git_worktree_of_a_registered_project_gets_the_same(self):
        self.register()
        self.note("For the next session.", "director")
        git(self.alpha, "init", "-q")
        git(self.alpha, "add", "room.toml")
        git(self.alpha, "commit", "-q", "-m", "one")
        tree = self.tmp / "trees" / "alpha-wt"
        git(self.alpha, "worktree", "add", "-q", str(tree), "-b", "side")
        self.assertEqual(self.added(tree), self.added(self.alpha))
        self.assertIn("1, from director", self.added(tree))

    def test_notes_before_the_last_attach_are_read_and_a_folder_with_nothing_new_gives_one_line(self):
        self.register()
        self.note("Old news.", "director", at=time.time() - 3600)
        ledger = Ledger()
        ledger.mark("alpha", handshake.ATTACHED + "alpha", at=time.time() - 60)
        ledger.close()
        text = self.added(self.alpha)
        self.assertEqual(text, f"Common Room keeps alpha's state in `{self.folder() / 'digest.md'}`.")
        self.assertEqual(text, TEMPLATE.read_text(encoding="utf-8").split("\n")[0].replace(
            "{{project}}", "alpha").replace("{{digest}}", str(self.folder() / "digest.md")))
        self.note("New since.", "beta")
        self.assertIn("Unread notes in its `inbox/`: 1, from beta.", self.added(self.alpha))

    def test_another_readers_attach_never_marks_this_projects_notes_read(self):
        self.register()
        self.note("For alpha.", "director", at=time.time() - 3600)
        ledger = Ledger()
        for who in ("director", "beta", "desk"):
            ledger.mark("alpha", handshake.ATTACHED + who, at=time.time() - 60)
        ledger.close()
        self.assertIn("Unread notes in its `inbox/`: 1, from director.", self.added(self.alpha))

    def test_a_sender_does_not_see_its_own_notes_as_unread(self):
        self.register()
        self.note("Mine.", "alpha")
        self.note("Also mine.", "Alpha")
        self.assertNotIn("Unread", self.added(self.alpha))

    def test_attach_from_the_projects_folder_reads_but_as_director_does_not(self):
        self.register()
        self.note("For alpha.", "director", at=time.time() - 3600)
        for argv, cwd in ((["attach", "alpha", "--as", "director"], self.elsewhere), (["attach", "alpha"], self.elsewhere)):
            self.assertEqual(self.room(argv, cwd), 0)
            self.assertIn("1, from director", self.added(self.alpha))
        self.assertEqual(self.room(["attach"], self.alpha), 0)
        self.assertNotIn("Unread notes in its `inbox/`: 1", self.added(self.alpha))
        self.note("Again.", "director")
        self.assertIn("1, from director", self.added(self.alpha))
        with mock.patch("time.time", return_value=time.time() + 10):   # notes are dated to the second
            self.assertEqual(self.room(["notes"], self.alpha), 0)
        self.assertNotIn("Unread notes in its `inbox/`: 1", self.added(self.alpha))

    def test_the_hook_paragraph_alone_does_not_mark_anything_read(self):
        self.register()
        self.note("For alpha.", "director")
        self.added(self.alpha)
        self.assertIn("1, from director", self.added(self.alpha))

    def test_a_written_charter_with_no_notes_gives_the_paragraph(self):
        self.register()
        self.assertEqual(self.added(self.alpha).count("\n"), 0)   # never attached: no folder, no notes, one line
        where = handshake.prepare(registry.load().projects["alpha"])
        self.assertEqual(self.added(self.alpha).count("\n"), 0)   # the charter holds only its headings
        (where / "charter.md").write_text("# Charter: alpha\n\n## The outcome, and why\nReaders.\n", encoding="utf-8")
        self.assertIn("Unread notes in its `inbox/`: 0.", self.added(self.alpha))


class Silent(Hook):
    def test_an_unregistered_folder_adds_nothing(self):
        self.register()
        self.note("x", "director")
        self.assertEqual(self.added(self.elsewhere), "")

    def test_an_empty_or_missing_registry_adds_nothing_and_writes_nothing(self):
        self.assertEqual(self.added(self.alpha), "")
        self.assertFalse(self.home.exists())
        registry.save(registry.Registry())
        self.assertEqual(self.added(self.alpha), "")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["projects.toml"])

    def test_a_room_workers_folder_adds_nothing(self):
        self.register()
        self.note("x", "director")
        tree = self.alpha / "wt"
        tree.mkdir()
        ledger = Ledger()
        ledger.db.execute("INSERT INTO runs (id, project, role, provider, started_at, status, worktree) "
                          "VALUES ('r1', 'alpha', 'builder', 'claude', ?, 'running', ?)", (time.time(), str(tree)))
        ledger.db.commit()
        ledger.close()
        self.assertEqual(self.added(tree), "")
        self.assertNotEqual(self.added(self.alpha), "")
        with mock.patch.dict(os.environ, {"COMMONROOM_WORKER": "1"}):
            self.assertEqual(self.added(self.alpha), "")

    def test_malformed_input_prints_nothing_and_exits_0(self):
        self.register()
        self.note("x", "director")
        for stdin in ("", "not json", "[]", '"text"', "{", '{"cwd": 5}', '{"cwd": null}',
                      json.dumps({"cwd": str(self.tmp / "gone")}),
                      json.dumps({"cwd": str(self.alpha), "hook_event_name": "PreToolUse"})):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.run_hook(stdin), (0, "", ""))
        (self.home / "projects.toml").write_text("[projects.alpha\n", encoding="utf-8")   # a broken registry
        self.assertEqual(self.added(self.alpha), "")

    def test_a_hook_past_its_time_limit_prints_nothing_and_exits_0(self):
        self.register()
        self.note("x", "director")
        with mock.patch.object(hook, "context", side_effect=lambda data: time.sleep(5) or "late"):
            began = time.monotonic()
            result = self.run_hook(json.dumps({"cwd": str(self.alpha)}), limit=0.2)
            self.assertLess(time.monotonic() - began, 2)
        self.assertEqual(result, (0, "", ""))

    def test_any_other_event_name_adds_nothing(self):
        self.register()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(self.alpha)}))):
            self.assertEqual(cli.main(["hook", "stop"]), 0)
            self.assertEqual(cli.main(["hook"]), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


class Beside(Hook):
    """Axiom A0: the hook only adds context. It runs no model, no network and no board, and writes nothing."""

    def test_the_output_is_added_context_and_nothing_else(self):
        self.register()
        self.note("x", "director")
        code, out, err = self.run_hook(json.dumps({"session_id": "s", "cwd": str(self.alpha),
                                                   "hook_event_name": "SessionStart", "source": "resume"}))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(list(json.loads(out)), ["hookSpecificOutput"])
        self.assertEqual(sorted(json.loads(out)["hookSpecificOutput"]), ["additionalContext", "hookEventName"])
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "SessionStart")

    @unittest.skipUnless(GIT, "git is not installed")
    def test_only_git_rev_parse_runs_and_no_board_or_network_is_reached(self):
        self.register()
        self.note("x", "director")
        git(self.alpha, "init", "-q")
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.home.rglob("*") if p.is_file()}
        ran, real = [], subprocess.Popen.__init__

        def popen(proc, args, *a, **k):
            ran.append(list(args))
            return real(proc, args, *a, **k)

        def refuse(*a, **k):
            raise AssertionError("the hook reached a board or the network")

        with mock.patch.object(subprocess.Popen, "__init__", popen), \
                mock.patch.object(board, "gh", refuse), \
                mock.patch.object(socket.socket, "connect", refuse):
            text = self.added(self.alpha)
        self.assertIn("1, from director", text)
        self.assertTrue(ran)
        self.assertEqual([args[:2] for args in ran], [["git", "rev-parse"]] * len(ran))
        after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_the_plugin_and_this_repos_settings_declare_one_session_start_command_and_nothing_else(self):
        plugin = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
                         ["name"], "commonroom")
        self.assertEqual(list(plugin["hooks"]), ["SessionStart"])
        self.assertEqual(list(settings["hooks"]), ["SessionStart"])
        [group] = plugin["hooks"]["SessionStart"]
        self.assertEqual(list(group), ["hooks"])   # no matcher: every start, resume, clear and compaction
        [command] = group["hooks"]
        self.assertEqual(command, {"type": "command", "command": "python", "timeout": 5,
                                   "args": ["${CLAUDE_PLUGIN_ROOT}/../room.py", "hook", "session-start"]})
        mine = json.loads(json.dumps(settings["hooks"]).replace("${CLAUDE_PROJECT_DIR}", "${CLAUDE_PLUGIN_ROOT}/.."))
        self.assertEqual(mine, plugin["hooks"])


class HandshakeOnly(Hook):
    """Axiom A9: the text says what is there and how to reply, never how to work."""

    METHOD = re.compile(r"\b(you|your|should|must|always|never|please|first|before|after|make sure|remember|"
                        r"think|plan|step|test|verify|check|read|review|consider|try|ensure|start|begin)\b", re.I)

    def test_the_text_names_the_folder_the_notes_and_how_to_reply_and_no_method(self):
        self.register()
        self.note("Think step by step and always test first.", "director")
        (handshake.folder_of(registry.load().projects["alpha"]) / "charter.md").write_text(
            "# Charter\n\nYou must plan first.\n", encoding="utf-8")
        text = self.added(self.alpha)
        template = TEMPLATE.read_text(encoding="utf-8") + (ROOT / "commonroom/ways/handshake/reply.md").read_text(
            encoding="utf-8")
        for words in (text, template):
            self.assertIsNone(self.METHOD.search(words), words)
        sentences = [s for s in re.split(r"(?<=\.)\s+(?=[A-Z])", text) if s]
        self.assertEqual(len(sentences), 3, sentences)
        about = (("state in", "digest.md"), ("Unread notes", "inbox/"), ("To reply", "room note alpha"))
        for sentence, (opening, subject) in zip(sentences, about):
            self.assertTrue(sentence.startswith(("Common Room", opening)) and subject in sentence, sentence)


if __name__ == "__main__":
    unittest.main()
