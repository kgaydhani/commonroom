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
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from commonroom import board, cli, handshake, hook, places, policy, registry
from commonroom.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "commonroom" / "ways" / "handshake" / "session-start.md"
GIT = shutil.which("git")
LIVE_LIMIT = hook.LIMIT   # the hook's real limit, read before any test patches it
TEST_LIMIT = 30.0         # seconds: a loaded machine must not empty the output of a test that expects some


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
        limit = mock.patch.object(hook, "LIMIT", TEST_LIMIT)   # the one place the tests' hook limit is set
        limit.start()
        self.addCleanup(limit.stop)
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

    def run_hook(self, stdin, limit=TEST_LIMIT):
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

    def test_a_filtered_notes_list_by_kind_does_not_mark_the_inbox_read(self):
        self.register()
        self.note("For alpha.", "director", at=time.time() - 3600)
        with mock.patch("time.time", return_value=time.time() + 10):
            self.assertEqual(self.room(["notes", "--kind", "decision"], self.alpha), 0)
        self.assertIn("1, from director", self.added(self.alpha))

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


class Places(Hook):
    """commonroom#571: a known place that is no project gets one line; everything else is as before."""
    def know(self, folder, description=""):
        key = places.sessions.path_key(folder)
        data = places.load()
        data["places"][key] = {"path": str(folder), "last": 1.0, "sessions": ["s"], "description": description}
        places.save(data)

    def test_a_known_place_gets_one_short_line_with_its_name_and_description(self):
        self.know(self.elsewhere, "the owner's scratch notes")
        text = self.added(self.elsewhere)
        self.assertEqual(text, "This folder is `elsewhere`, a place Common Room knows: "
                               "the owner's scratch notes. `room card` shows it.")
        self.assertNotIn(chr(10), text)

    def test_a_place_without_a_description_and_a_folder_inside_a_place(self):
        self.know(self.elsewhere)
        inner = self.elsewhere / "sub"
        inner.mkdir()
        self.assertEqual(self.added(inner), "This folder is `elsewhere`, a place Common Room knows. "
                                                 "`room card` shows it.")

    def test_the_line_names_room_card_and_the_command_exists(self):
        self.know(self.elsewhere)
        self.assertIn("`room card` shows it.", self.added(self.elsewhere))
        self.assertTrue(callable(cli.cmd_card))

    def test_an_unknown_folder_and_a_worker_get_nothing(self):
        self.know(self.elsewhere)
        other = self.tmp / "other"
        other.mkdir()
        self.assertEqual(self.added(other), "")
        with mock.patch.dict(os.environ, {"COMMONROOM_WORKER": "1"}):
            self.assertEqual(self.added(self.elsewhere), "")

    def test_a_corrupt_places_file_adds_nothing_and_exits_0(self):
        self.home.mkdir(parents=True)
        (self.home / places.FILENAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.added(self.elsewhere), "")
        with mock.patch.object(places, "find", side_effect=RuntimeError("boom")):
            self.know(self.elsewhere)
            self.assertEqual(self.added(self.elsewhere), "")

    def test_a_registered_project_gets_exactly_the_text_it_got_before_even_with_places_known(self):
        self.register()
        self.know(self.elsewhere, "x")
        self.note("Please write your charter.", "director")
        before = self.added(self.alpha / "src")
        self.assertEqual(before, handshake.fill_way(
            hook.SESSION_START, project="alpha", digest=str(self.folder() / "digest.md"), notes="1, from director",
            reply=handshake.reply_line(registry.load().projects["alpha"])))

    def test_listing_a_place_writes_nothing_into_it(self):
        self.know(self.elsewhere)
        self.added(self.elsewhere)
        self.assertEqual(list(self.elsewhere.iterdir()), [])


class RecentPlaces(Hook):
    """commonroom#578: a place's session start also names the other places used in the last 14 days."""
    know = Places.know

    def know_at(self, name, days_ago, description="", make=True):
        folder = self.tmp / name
        if make:
            folder.mkdir(exist_ok=True)
        data = places.load()
        data["places"][places.sessions.path_key(folder)] = {
            "path": str(folder), "last": time.time() - days_ago * 86400, "sessions": ["s"], "description": description}
        places.save(data)
        return folder

    def test_the_other_recent_places_are_listed_one_line_each_newest_first(self):
        self.know(self.elsewhere, "scratch")
        a = self.know_at("older", 10, "old notes")
        b = self.know_at("newer", 1)
        lines = self.added(self.elsewhere).split("\n")
        self.assertEqual(lines[1], "Other places used in the last 14 days:")
        self.assertEqual(lines[2:], [f"- `newer` ({b})", f"- `older` ({a}): old notes"])

    def test_old_gone_and_current_places_and_the_count_limit_are_left_out(self):
        self.know(self.elsewhere)
        self.know_at("stale", 15)
        self.know_at("gone", 1, make=False)
        for i in range(7):
            self.know_at(f"p{i}", i / 10)
        text = self.added(self.elsewhere)
        self.assertNotIn("stale", text)
        self.assertNotIn("gone", text)
        self.assertNotIn("- `elsewhere`", text)
        self.assertEqual([l[:5] for l in text.split("\n")[2:]], [f"- `p{i}" for i in range(5)])

    def test_a_count_of_0_turns_the_list_off(self):
        self.know(self.elsewhere, "x")
        self.know_at("other", 1)
        with mock.patch.dict(policy.DEFAULTS, {"places_listed": 0}):
            self.assertEqual(self.added(self.elsewhere).count("\n"), 0)

    def test_the_whole_text_stays_inside_the_budget_even_with_long_paths_and_descriptions(self):
        self.know(self.elsewhere, "d" * 400)
        for i in range(8):
            self.know_at(f"{'n' * 150}{i}", 1, "d" * 500)
        text = self.added(self.elsewhere)
        self.assertTrue(text.split("\n")[0].startswith("This folder is `elsewhere`"))
        self.assertLessEqual(-(-len(text) // 4), policy.DEFAULTS["session_start_max_tokens"])
        self.assertIn("- `nnn", text)

    def test_the_longest_project_text_fits_the_budget_with_the_list_after_it(self):
        self.register()
        for n in range(8):
            self.know_at(f"{'n' * 150}{n}", 1, "d" * 500)
        for n in (0, 1, 2):
            self.note("Please write your charter.", "director" * 5)
        text = self.added(self.alpha)
        self.assertLessEqual(-(-len(text) // 4), policy.DEFAULTS["session_start_max_tokens"])
        self.assertTrue(text.startswith("Common Room keeps alpha"))
        self.assertIn("Other places", text)
        self.assertIn("- `nnn", text)

    def test_a_project_session_gets_the_list_without_its_own_folder(self):
        self.register()
        self.know(self.alpha)
        other = self.know_at("other", 1, "side")
        text = self.added(self.alpha)
        self.assertIn(f"- `other` ({other}): side", text)
        self.assertNotIn("- `alpha`", text)


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


class Margin(Hook):
    def test_a_slow_hook_still_gives_its_output_under_the_test_limit_and_the_live_limit_is_unchanged(self):
        self.register()
        real = hook.context

        def slow(data):
            time.sleep(2.5)
            return real(data)

        with mock.patch.object(hook, "context", slow):
            self.assertIn("alpha", self.added(self.alpha))
        self.assertEqual(LIVE_LIMIT, 2.0)


class Beside(Hook):
    """A0: context hooks only add context; only the owner's limits may block own sessions (test_limits)."""

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

    def test_the_plugin_declares_context_hooks_and_only_the_limits_pre_tool_hook(self):
        plugin = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
                         ["name"], "commonroom")
        # commonroom#583: PreToolUse is the one hook that may block, and only what the owner's own limits.txt names
        self.assertEqual(list(plugin["hooks"]), ["SessionStart", "UserPromptSubmit", "PreToolUse"])
        self.assertEqual(list(settings["hooks"]), ["SessionStart", "UserPromptSubmit"])
        pre_tool = plugin["hooks"].pop("PreToolUse")
        # the Codex form (`commandWindows`, commonroom#397) is checked in PluginHooksForCodex; Claude Code's form is unchanged
        plugin = json.loads(json.dumps(plugin))
        for groups in plugin["hooks"].values():
            for group in groups:
                for entry in group["hooks"]:
                    entry.pop("commandWindows")
        self.assertEqual(pre_tool, [{"matcher": "Bash|PowerShell|Read|Edit|Write|MultiEdit|NotebookEdit",
                                    "hooks": [{"type": "command", "command": "python", "timeout": 5,
                                               "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "pre-tool"],
                                               "commandWindows": 'if exist "%PLUGIN_ROOT%\\hooks\\run.py" python '
                                                                 '"%PLUGIN_ROOT%\\hooks\\run.py" pre-tool'}]}])
        [prompt] = plugin["hooks"]["UserPromptSubmit"]
        self.assertEqual(prompt["hooks"], [{"type": "command", "command": "python", "timeout": 5,
                                            "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "prompt-submit"]}])
        [group] = plugin["hooks"]["SessionStart"]
        self.assertEqual(list(group), ["hooks"])   # no matcher: every start, resume, clear and compaction
        [command] = group["hooks"]
        self.assertEqual(command, {"type": "command", "command": "python", "timeout": 5,
                                   "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "session-start"]})
        # this repo's own settings run room.py directly; the plugin runs the launcher, which runs the same `room.py hook <event>`
        mine = json.loads(json.dumps(settings["hooks"]).replace("${CLAUDE_PROJECT_DIR}/room.py\", \"hook\", ", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py\", "))
        self.assertEqual(mine, plugin["hooks"])


class PluginHooksForCodex(unittest.TestCase):
    """commonroom#397: one hooks.json that Claude Code (command + args) and Codex (commandWindows) both run."""

    def test_every_entry_has_both_forms_naming_the_same_hook(self):
        plugin = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        wanted = {"SessionStart": "session-start", "UserPromptSubmit": "prompt-submit", "PreToolUse": "pre-tool"}
        self.assertEqual(list(plugin["hooks"]), list(wanted))
        for event, name in wanted.items():
            entries = [e for g in plugin["hooks"][event] for e in g["hooks"]]
            self.assertTrue(entries)
            for entry in entries:
                self.assertEqual(entry["args"][-1], name)
                self.assertTrue(entry["commandWindows"].endswith(f"run.py\" {name}"), entry["commandWindows"])
                self.assertIn("hooks\\run.py", entry["commandWindows"])
                self.assertIn("%PLUGIN_ROOT%", entry["commandWindows"])
                self.assertNotIn("${", entry["commandWindows"])   # cmd does not expand ${VAR}

    def run_codex_form(self, cmd, plugin_root):
        # as Codex does: one raw command line through `cmd /C "<command>"` (list form would escape the quotes as \\")
        return subprocess.run(f'cmd /C "{cmd}"', env={**os.environ, "PLUGIN_ROOT": str(plugin_root), "COMMONROOM_HOME": str(Path(plugin_root).parent / "nohome")},
                              capture_output=True, text=True, input='{"x":1}')

    def test_codex_form_runs_room_py_beside_the_plugin_and_is_silent_without_it(self):
        if os.name != "nt":
            self.skipTest("cmd.exe only")
        plugin = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "a root with space"
            shutil.copytree(ROOT / "plugin", root / "plugin")
            (root / "room.py").write_text("import sys\nprint('ARGS', sys.argv[1:], sys.stdin.read())\n",
                                          encoding="utf-8")
            for event, name in (("SessionStart", "session-start"), ("UserPromptSubmit", "prompt-submit")):
                cmd = plugin["hooks"][event][0]["hooks"][0]["commandWindows"]
                done = self.run_codex_form(cmd, root / "plugin")
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.strip(), f"ARGS ['hook', '{name}'] " + '{"x":1}')
                shutil.copytree(ROOT / "plugin", Path(d) / "cache" / "p", dirs_exist_ok=True)
                done = self.run_codex_form(cmd, Path(d) / "cache" / "p")
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))


class PluginInstalled(Hook):
    """commonroom#565: the plugin copied out of the checkout (a user-scope install) gives the answer it gives in place."""

    def setUp(self):
        super().setUp()
        self.register()
        self.copy = self.tmp / "cache" / "commonroom"
        shutil.copytree(ROOT / "plugin", self.copy, ignore=shutil.ignore_patterns("__pycache__"))
        self.hooks = json.loads((self.copy / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.scratch_home = self.tmp / "userhome"
        self.scratch_home.mkdir()

    def run_declared(self, plugin_root, event, cwd, data):
        """Run the Claude Code form of `event`'s command, with CLAUDE_PLUGIN_ROOT expanded, from `cwd`."""
        entry = self.hooks[event][0]["hooks"][0]
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root)) for a in entry["args"]]
        env = {**os.environ, "HOME": str(self.scratch_home), "USERPROFILE": str(self.scratch_home)}
        return subprocess.run([sys.executable, *args], cwd=cwd, env=env, input=json.dumps(data), capture_output=True,
                              text=True, timeout=60)

    def start(self, plugin_root):
        data = {"session_id": "s1", "cwd": str(self.alpha), "hook_event_name": "SessionStart", "source": "startup"}
        return self.run_declared(plugin_root, "SessionStart", self.elsewhere, data)

    def test_a_copy_outside_the_repo_answers_as_the_plugin_in_place_does(self):
        (self.home).mkdir(parents=True, exist_ok=True)
        (self.home / "room-path").write_text(str(ROOT / "room.py"), encoding="utf-8")
        in_place = self.start(ROOT / "plugin")
        copied = self.start(self.copy)
        self.assertEqual((in_place.returncode, in_place.stderr), (0, ""))
        self.assertIn("alpha", in_place.stdout)
        self.assertEqual((copied.returncode, copied.stdout, copied.stderr),
                         (in_place.returncode, in_place.stdout, in_place.stderr))

    def test_in_place_is_the_same_as_running_room_py_directly(self):
        data = {"session_id": "s1", "cwd": str(self.alpha), "hook_event_name": "SessionStart", "source": "startup"}
        direct = subprocess.run([sys.executable, str(ROOT / "room.py"), "hook", "session-start"], cwd=self.elsewhere,
                                input=json.dumps(data), capture_output=True, text=True, timeout=60)
        self.assertEqual(self.start(ROOT / "plugin").stdout, direct.stdout)
        self.assertTrue(direct.stdout)

    def test_a_copy_that_cannot_find_the_room_exits_0_and_prints_nothing(self):
        for event in ("SessionStart", "UserPromptSubmit"):
            done = self.run_declared(self.copy, event, self.elsewhere, {"cwd": str(self.alpha)})
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""), event)
        (self.home).mkdir(parents=True, exist_ok=True)
        for bad in (str(self.tmp / "missing" / "room.py"), ""):
            (self.home / "room-path").write_text(bad, encoding="utf-8")
            done = self.start(self.copy)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""), repr(bad))


class MarketplaceFile(unittest.TestCase):
    def test_lists_the_plugin_at_its_folder(self):
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertTrue(market["name"] and market["owner"]["name"])
        [entry] = market["plugins"]
        self.assertEqual(entry["name"], json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json")
                                                    .read_text(encoding="utf-8"))["name"])
        self.assertTrue((ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file())


class ColdReturn(Hook):
    """commonroom#367: a UserPromptSubmit hook that, at a cold return to a large context, says what continuing costs."""

    NOW = 1_800_000_000.0

    def transcript(self, minutes_ago, context, name="t.jsonl"):
        """A fixture transcript whose last assistant turn is `minutes_ago` old with `context` tokens in context."""
        def line(ago, ctx):
            at = datetime.fromtimestamp(self.NOW - ago * 60, timezone.utc).isoformat()
            return json.dumps({"type": "assistant", "timestamp": at, "sessionId": "s1", "cwd": str(self.alpha),
                               "message": {"id": f"m{ago}", "model": "m", "content": [{"type": "text", "text": "SECRET"}],
                                           "usage": {"input_tokens": 1, "output_tokens": 5,
                                                     "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx}}})
        path = self.tmp / name
        path.write_text(json.dumps({"type": "user"}) + "\n" + line(minutes_ago + 5, 10) + "\n"
                        + line(minutes_ago, context) + "\n", encoding="utf-8")
        return path

    def prompt(self, transcript, cwd=None, event="UserPromptSubmit"):
        data = {"session_id": "s1", "cwd": str(cwd or self.alpha), "hook_event_name": event,
                "transcript_path": str(transcript), "prompt": "hi"}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),                 mock.patch("sys.stdin", io.StringIO(json.dumps(data))),                 mock.patch.object(hook.time, "time", return_value=self.NOW):
            code = cli.main(["hook", "prompt-submit"])
        self.assertEqual((code, err.getvalue()), (0, ""))
        return out.getvalue()

    def brief(self, name="20260101-000000-000000-s0.md", taken=False):
        p = registry.load().projects["alpha"]
        folder = handshake.prepare(p) / "handoffs"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text("---\nfrom_session: s0\n---\nx\n", encoding="utf-8")
        if taken:
            ledger = Ledger()
            try:
                ledger.mark("alpha", handshake.TOOK + name, at=self.NOW - 3600)
            finally:
                ledger.close()

    def test_a_taken_brief_is_not_named(self):
        self.register()
        self.brief(taken=True)
        text = json.loads(self.prompt(self.transcript(90, 300000)))["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("20260101-000000-000000-s0.md", text)
        self.assertIn("room handoff alpha", text)

    def test_the_newest_untaken_brief_is_named_when_a_newer_one_was_taken(self):
        self.register()
        self.brief("20260101-000000-000000-old.md")
        self.brief("20260102-000000-000000-new.md", taken=True)
        text = json.loads(self.prompt(self.transcript(90, 300000)))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("handoffs/20260101-000000-000000-old.md", text)
        self.assertNotIn("20260102-000000-000000-new.md", text)

    def test_a_cold_return_to_a_large_context_adds_a_paragraph_naming_the_brief(self):
        self.register()
        self.brief()
        out = json.loads(self.prompt(self.transcript(90, 300000)))
        self.assertEqual(list(out), ["hookSpecificOutput"])   # context only: never a decision or a block (A0)
        self.assertEqual(sorted(out["hookSpecificOutput"]), ["additionalContext", "hookEventName"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        text = out["hookSpecificOutput"]["additionalContext"]
        for part in ("90 minutes", "300k", "handoffs/20260101-000000-000000-s0.md", "fresh session"):
            self.assertIn(part, text)
        self.assertNotIn("SECRET", text)

    def test_with_no_brief_it_names_the_handoff_command(self):
        self.register()
        text = json.loads(self.prompt(self.transcript(90, 300000)))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("room handoff alpha", text)

    def test_a_warm_return_or_a_small_context_adds_nothing(self):
        self.register()
        self.assertEqual(self.prompt(self.transcript(10, 300000)), "")
        self.assertEqual(self.prompt(self.transcript(90, 40000)), "")

    def test_a_missing_or_malformed_transcript_adds_nothing(self):
        self.register()
        bad = self.tmp / "bad.jsonl"
        bad.write_text("not json\n{\n", encoding="utf-8")
        for path in (self.tmp / "none.jsonl", bad):
            self.assertEqual(self.prompt(path), "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO('{"hook_event_name": "UserPromptSubmit"}')):
            self.assertEqual(cli.main(["hook", "prompt-submit"]), 0)
        self.assertEqual(out.getvalue(), "")

    def test_a_worker_a_foreign_folder_or_another_event_adds_nothing(self):
        self.register()
        path = self.transcript(90, 300000)
        self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")
        self.assertEqual(self.prompt(path, event="SessionStart"), "")
        with mock.patch.dict(os.environ, {"COMMONROOM_WORKER": "1"}):
            self.assertEqual(self.prompt(path), "")
        self.assertNotEqual(self.prompt(path), "")

    def test_only_the_tail_is_read(self):
        self.register()
        path = self.transcript(90, 300000)
        path.write_bytes(b"x" * 2000 + b"\n" + path.read_bytes())
        self.assertEqual(hook.last_turn(path, 400)[1], 300001)
        self.assertIsNone(hook.last_turn(path, 5))

    def rollout(self, minutes_ago, context, name="r.jsonl", extra=()):
        """A synthetic Codex rollout (cli_version 0.167) whose last token_count is `minutes_ago` old."""
        def stamp(ago):
            return datetime.fromtimestamp(self.NOW - ago * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        def count(ago, ctx):
            return {"timestamp": stamp(ago), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 999999}, "last_token_usage": {
                    "input_tokens": ctx, "cached_input_tokens": ctx // 2, "output_tokens": 5, "total_tokens": ctx + 5},
                "model_context_window": 475000}}}
        lines = [{"timestamp": stamp(minutes_ago + 9), "type": "session_meta",
                  "payload": {"id": "x", "cwd": "/nowhere", "cli_version": "0.167.0-alpha.4.1"}},
                 {"timestamp": stamp(minutes_ago + 8), "type": "response_item",
                  "payload": {"type": "message", "content": [{"text": "SECRET"}]}},
                 count(minutes_ago + 5, 10), count(minutes_ago, context), *extra]
        path = self.tmp / name
        path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        return path

    def test_a_codex_rollout_gives_the_same_paragraph_as_a_claude_transcript(self):
        self.register()
        self.brief()
        claude = self.prompt(self.transcript(90, 300000))
        codex = self.prompt(self.rollout(90, 300000))
        self.assertNotEqual(codex, "")
        self.assertEqual(codex, claude)   # same time and context -> same paragraph
        self.assertEqual(hook.last_turn(self.rollout(90, 123456)), (self.NOW - 90 * 60, 123456))
        self.assertNotIn("SECRET", codex)

    def test_a_rollout_is_told_by_its_lines_not_its_path(self):
        path = self.rollout(90, 300000, name="transcript.jsonl")
        self.assertEqual(hook.last_turn(path)[1], 300000)
        self.assertEqual(hook.last_turn(path, 600)[1], 300000)   # the head is not in the tail

    def test_a_rollout_with_no_usable_token_count_adds_nothing(self):
        self.register()
        head = {"timestamp": datetime.fromtimestamp(self.NOW - 600, timezone.utc).isoformat(), "type": "session_meta", "payload": {"cli_version": "0.167.0"}}
        odd = [{"type": "future_thing", "payload": {}}, {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
               {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {}}}}]
        for name, lines in (("a.jsonl", [head]), ("b.jsonl", [head, *odd])):
            path = self.tmp / name
            path.write_text("".join(json.dumps(x) + "\n" for x in lines) + "garbage\n", encoding="utf-8")
            self.assertIsNone(hook.last_turn(path))
            self.assertEqual(self.prompt(path), "")

    def test_the_text_has_no_method_words(self):
        template = (ROOT / "commonroom/ways/handshake/cold-return.md").read_text(encoding="utf-8")
        self.assertIsNone(HandshakeOnly.METHOD.search(template), template)


class Compacted(Hook):
    """commonroom#410: a SessionStart after a second compaction offers a fresh start; it never blocks."""

    def transcript(self, boundaries, name="c.jsonl"):
        lines = [json.dumps({"type": "user"})]
        for _ in range(boundaries):
            lines += [json.dumps({"type": "system", "subtype": "compact_boundary"}), json.dumps({"type": "user"})]
        path = self.tmp / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def start(self, source, path):
        return self.added(self.alpha, source=source, transcript_path=str(path))

    def test_two_boundaries_after_a_compaction_add_the_paragraph_naming_the_count(self):
        self.register()
        plain = self.added(self.alpha, source="startup")
        text = self.start("compact", self.transcript(2))
        self.assertTrue(text.startswith(plain))
        self.assertIn("compacted 2 times", text)
        self.assertIn("room handoff alpha --session <id> --reason <text>", text)
        self.assertIn("compacted 3 times", self.start("compact", self.transcript(3)))

    def test_the_newest_untaken_brief_is_named_instead_of_the_command(self):
        self.register()
        p = registry.load().projects["alpha"]
        folder = handshake.prepare(p) / "handoffs"
        folder.mkdir(exist_ok=True)
        (folder / "20260101-000000-000000-s0.md").write_text("---\nfrom_session: s0\n---\nx\n", encoding="utf-8")
        self.assertIn("handoffs/20260101-000000-000000-s0.md", self.start("compact", self.transcript(2)))

    def test_one_boundary_or_another_source_adds_nothing_new(self):
        self.register()
        plain = self.added(self.alpha, source="startup")
        self.assertEqual(self.start("compact", self.transcript(1)), plain)
        self.assertEqual(self.start("startup", self.transcript(3)), plain)
        self.assertEqual(self.start("resume", self.transcript(3)), plain)

    def test_a_missing_or_unreadable_transcript_adds_nothing_and_exits_0(self):
        self.register()
        plain = self.added(self.alpha, source="startup")
        self.assertEqual(self.start("compact", self.tmp / "none.jsonl"), plain)
        self.assertEqual(self.start("compact", self.tmp), plain)   # a folder cannot be read as a file
        self.assertEqual(self.added(self.alpha, source="compact"), plain)
        self.assertEqual(hook.compactions(self.tmp / "none.jsonl"), 0)

    def test_a_boundary_cut_by_a_chunk_edge_is_counted_once(self):
        mark = b'{"type":"system","subtype":"compact_boundary"}\n'
        path = self.tmp / "edge.jsonl"
        for pad in range(0, len(mark) + 2):
            path.write_bytes(b"x" * (64 - pad) + mark + mark)
            with mock.patch.object(hook, "CHUNK", 64):
                self.assertEqual(hook.compactions(path), 2, pad)

    def test_the_template_exists_and_has_no_method_words(self):
        template = (ROOT / "commonroom/ways/handshake/compacted.md").read_text(encoding="utf-8")
        self.assertIsNone(HandshakeOnly.METHOD.search(template), template)


class HandshakeOnly(Hook):
    """Axiom A9: the text says what is there and how to reply, never how to work."""

    METHOD = re.compile(r"\b(you|your|should|must|always|never|please|first|before|after|make sure|remember|"
                        r"think|plan|step|test|verify|check|read|review|consider|try|ensure|start|begin)\b", re.I)

    def without_paths(self, text):
        """`text` with this test's temp folder taken out: the check judges the hook's own words, not where the
        temporary home happens to be (a reviewer's TEMP is `<tmp>/review-<n>`, commonroom#277)."""
        for root in {str(self.tmp), str(self.tmp.resolve())}:
            text = text.replace(root, "TMP")
        return text

    def test_the_paths_are_taken_out_but_a_method_word_beside_them_is_kept(self):
        words = self.without_paths(f"You should read `{self.tmp / 'digest.md'}`.")
        self.assertNotIn(str(self.tmp), words)
        self.assertEqual([m.lower() for m in self.METHOD.findall(words)], ["you", "should", "read"])

    def test_the_text_names_the_folder_the_notes_and_how_to_reply_and_no_method(self):
        self.register()
        self.note("Think step by step and always test first.", "director")
        (handshake.folder_of(registry.load().projects["alpha"]) / "charter.md").write_text(
            "# Charter\n\nYou must plan first.\n", encoding="utf-8")
        text = self.added(self.alpha)
        template = TEMPLATE.read_text(encoding="utf-8") + (ROOT / "commonroom/ways/handshake/reply.md").read_text(
            encoding="utf-8")
        for words in (self.without_paths(text), template):
            self.assertIsNone(self.METHOD.search(words), words)
        sentences = [s for s in re.split(r"(?<=\.)\s+(?=[A-Z])", text) if s]
        self.assertEqual(len(sentences), 3, sentences)
        about = (("state in", "digest.md"), ("Unread notes", "inbox/"), ("To reply", "room note alpha"))
        for sentence, (opening, subject) in zip(sentences, about):
            self.assertTrue(sentence.startswith(("Common Room", opening)) and subject in sentence, sentence)


class HandshakeOnlyUnderAReviewFolder(HandshakeOnly):
    """The same checks with the temp root in a folder named like a reviewer's worktree (commonroom#277)."""

    def setUp(self):
        outer = tempfile.TemporaryDirectory()
        self.addCleanup(outer.cleanup)
        review = Path(outer.name) / "review-1"
        review.mkdir()
        temp = mock.patch.object(tempfile, "tempdir", str(review))
        temp.start()
        self.addCleanup(temp.stop)
        super().setUp()
        self.assertIn("review-1", str(self.tmp))


class FreshStart(Hook):
    """commonroom#580: a session older than N days, or one that worked in more than M folders, is offered a fresh
    start once. Everything is built in a temporary room home."""

    NOW = ColdReturn.NOW
    transcript, prompt = ColdReturn.transcript, ColdReturn.prompt

    def long_transcript(self, days, folders, name="long.jsonl"):
        def line(ago_days, cwd):
            at = datetime.fromtimestamp(self.NOW - ago_days * 86400, timezone.utc).isoformat()
            return json.dumps({"type": "user", "timestamp": at, "sessionId": "s1", "cwd": str(cwd)})
        rows = [line(days - i * days / max(folders, 1) / 2, self.tmp / f"f{i}") for i in range(folders)] or [line(days, self.alpha)]
        path = self.tmp / name
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    def test_an_old_session_is_offered_once_with_the_handover_command(self):
        self.register()
        path = self.long_transcript(3.5, 1)
        text = self.prompt(path)
        self.assertIn("3 days old", json.loads(text)["hookSpecificOutput"]["additionalContext"])
        self.assertIn("room handoff alpha --session s1 --reason", text)
        self.assertEqual(self.prompt(path), "")

    def test_a_wandering_session_is_offered_once_in_any_folder(self):
        path = self.long_transcript(0.5, 4)
        text = self.prompt(path, cwd=self.elsewhere)
        self.assertIn("4 folders wide", text)
        self.assertIn("room handoff <project> --session s1", text)
        self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")

    def test_below_the_thresholds_nothing_is_offered_and_nothing_is_written(self):
        path = self.long_transcript(2.9, 3)
        self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")
        self.assertFalse((self.home / hook.OFFERED).exists())

    def test_the_numbers_are_the_policy_ones(self):
        path = self.long_transcript(2.0, 2)
        with mock.patch.dict(policy.DEFAULTS, {"fresh_start_days": 1}):
            self.assertIn("days old", self.prompt(path, cwd=self.elsewhere))
        self.assertEqual((policy.DEFAULTS["fresh_start_days"], policy.DEFAULTS["fresh_start_places"]), (3, 3))

    def test_another_session_is_offered_on_its_own_and_a_worker_never(self):
        path = self.long_transcript(4, 1)
        with mock.patch.dict(os.environ, {"COMMONROOM_WORKER": "1"}):
            self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")
        self.assertIn("days old", self.prompt(path, cwd=self.elsewhere))
        self.assertEqual(hook.fresh_start({"session_id": "s2", "cwd": str(self.elsewhere), "transcript_path": str(path)}, self.NOW).count("days old"), 1)

    def test_a_missing_transcript_or_a_corrupt_record_adds_nothing(self):
        self.assertEqual(self.prompt(self.tmp / "none.jsonl", cwd=self.elsewhere), "")
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / hook.OFFERED).write_text("{not json", encoding="utf-8")
        self.assertIn("days old", self.prompt(self.long_transcript(4, 1), cwd=self.elsewhere))

    def old_cold_transcript(self):
        """A cold-return transcript whose first line is 4 days old."""
        path = self.transcript(90, 300000)
        at = datetime.fromtimestamp(self.NOW - 4 * 86400, timezone.utc).isoformat()
        path.write_text(json.dumps({"type": "user", "timestamp": at, "cwd": str(self.alpha)}) + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        return path

    def test_a_cold_return_keeps_its_text_and_the_offer_follows_it(self):
        self.register()
        text = json.loads(self.prompt(self.old_cold_transcript()))["hookSpecificOutput"]["additionalContext"]
        self.assertLess(text.index("prompt cache has expired"), text.index("days old"))

    def test_the_cold_return_text_survives_a_fresh_start_that_raises_or_is_slow(self):
        self.register()
        path = self.old_cold_transcript()
        with mock.patch.object(hook, "fresh_start", side_effect=OSError("disk")):
            out = self.prompt(path)
        self.assertIn("prompt cache has expired", out)
        self.assertNotIn("days old", out)
        with mock.patch.object(hook, "_span", side_effect=lambda *a, **k: time.sleep(3)):
            data = {"session_id": "s1", "cwd": str(self.alpha), "hook_event_name": "UserPromptSubmit", "transcript_path": str(path)}
            out, started = io.StringIO(), time.monotonic()
            with mock.patch.object(hook.time, "time", return_value=self.NOW):
                hook.main(io.StringIO(json.dumps(data)), out, limit=0.5, event="UserPromptSubmit")
        self.assertIn("prompt cache has expired", out.getvalue())
        self.assertLess(time.monotonic() - started, 2.5)

    def test_a_second_prompt_reads_only_the_bytes_added_since_the_first(self):
        path = self.long_transcript(1, 2)
        size, reads = path.stat().st_size, []
        real = open
        def spy(file, mode="r", *a, **k):
            handle = real(file, mode, *a, **k)
            if str(file) == str(path):
                reads.append(None)
                seek = handle.seek
                handle.seek = lambda off, *x: (reads.__setitem__(-1, off), seek(off, *x))[1]
            return handle
        with mock.patch.object(hook, "open", spy, create=True):
            self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "user", "cwd": str(self.tmp / "f9")}) + "\n")
            self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")
        self.assertEqual(reads[-2:], [0, size])

    def test_the_scan_goes_on_across_prompts_and_a_half_written_line_waits(self):
        path = self.long_transcript(0.5, 3)
        self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "cwd": str(self.tmp / "f9")}))   # no newline yet
        self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")
        with path.open("a", encoding="utf-8") as f:
            f.write("\n")
        self.assertIn("4 folders wide", self.prompt(path, cwd=self.elsewhere))
        self.assertEqual(self.prompt(path, cwd=self.elsewhere), "")


if __name__ == "__main__":
    unittest.main()
