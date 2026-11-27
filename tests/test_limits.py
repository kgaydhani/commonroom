"""Owner-chosen limits only; all homes, settings, projects and ledgers are scratch."""
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from commonroom import cli, hook, install, ledger, limits, pretool, registry
import test_install


class LimitsTest(unittest.TestCase):
    run_cmd = test_install.InstallTest.run_cmd
    put = test_install.InstallTest.put

    def setUp(self):
        test_install.InstallTest.setUp(self)
        scratch = Path(self.tmp.name) / "user"
        scratch.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(scratch), "USERPROFILE": str(scratch)})
        env.start()
        self.addCleanup(env.stop)
        self.db = ledger.Ledger()
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.path.parent, self.home)
        self.file = self.home / "limits.txt"

    def choose(self, text):
        self.file.write_text(text, encoding="utf-8", newline="\n")

    def settings_data(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_a0_only_the_owners_list_can_block_own_sessions(self):
        self.put('{"permissions":{"deny":["WebFetch"]}}')
        self.choose("# An example is not a limit\n# Bash(pkill *)\nBash(git push --force *)\n")
        self.run_cmd(install.cmd_install)
        self.assertEqual(self.settings_data()["permissions"]["deny"], ["WebFetch", "Bash(git push --force *)"])
        project = Path(self.tmp.name) / "orchard"
        project.mkdir()
        (project / "room.toml").write_text('[project]\nname = "orchard"\n', encoding="utf-8", newline="\n")
        registry.save(registry.Registry({"orchard": registry.Project("orchard", str(project), 0)}))
        transcript = self.claude / "projects" / "orchard" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "assistant", "timestamp": "2000-01-01T00:00:00+00:00",
                              "sessionId": "scratch", "cwd": str(project),
                              "message": {"usage": {"input_tokens": 300000}}}) + "\n", encoding="utf-8")
        with mock.patch.dict(os.environ), mock.patch.object(hook, "LIMIT", 30):
            os.environ.pop("COMMONROOM_WORKER", None)
            for command, expected in (("git push --force origin topic", 2), ("git push origin topic", 0),
                                      ("pkill -f example", 0)):
                for event in ("session-start", "prompt-submit", "pre-tool"):
                    with self.subTest(command=command, event=event):
                        out, err = io.StringIO(), io.StringIO()
                        data = {"cwd": str(project), "tool_name": "Bash", "tool_input": {"command": command},
                                "transcript_path": str(transcript)}
                        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))), \
                                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                            code = cli.main(["hook", event])
                        self.assertEqual(code, expected if event == "pre-tool" else 0)
                        if event == "pre-tool":
                            self.assertEqual(out.getvalue(), "")
                            self.assertEqual(bool(err.getvalue()), expected == 2)
                        else:
                            self.assertEqual(err.getvalue(), "")
                            payload = json.loads(out.getvalue())
                            self.assertEqual(list(payload), ["hookSpecificOutput"])
                            self.assertEqual(sorted(payload["hookSpecificOutput"]),
                                             ["additionalContext", "hookEventName"])
        self.choose("")
        self.run_cmd(install.cmd_install)
        self.assertEqual(self.settings_data()["permissions"]["deny"], ["WebFetch"])
        with mock.patch.dict(os.environ, {"COMMONROOM_WORKER": ""}):
            self.assertEqual(pretool.hook(json.dumps({"tool_name": "Bash",
                             "tool_input": {"command": "git push --force origin topic"}})), (0, ""))

    def test_default_list_is_readable_empty_and_changes_no_project_permissions(self):
        projects = [Path(self.tmp.name) / n for n in ("orchard", "pottery")]
        original = b'{"permissions":{"allow":["Read"],"deny":["WebFetch"]}}\n'
        for project in projects:
            (project / ".claude").mkdir(parents=True)
            (project / ".claude/settings.json").write_bytes(original)
        self.put(original.decode())
        self.run_cmd(install.cmd_install)
        self.assertEqual(limits.load(self.file), [])
        self.assertGreaterEqual(self.file.read_text().count("# Bash("), 1)
        self.assertIn("# Read(", self.file.read_text())
        self.assertEqual(self.settings_data()["permissions"], json.loads(original)["permissions"])
        for project in projects:
            self.assertEqual((project / ".claude/settings.json").read_bytes(), original)

    def test_dry_run_creates_nothing_and_existing_list_is_not_overwritten(self):
        self.run_cmd(install.cmd_install, dry=True)
        self.assertFalse(self.file.exists())
        self.assertFalse(self.settings.exists())
        self.choose("# my choices\n Read(~/.ssh/**) \n\nRead(~/.ssh/**)\nWebFetch\n")
        original = self.file.read_bytes()
        _, out = self.run_cmd(install.cmd_install, dry=True)
        for rule in ("Read(~/.ssh/**)", "WebFetch"):
            self.assertIn(f"permissions.deny: {rule} — a deny rule cannot be lifted from inside a session", out)
        self.assertFalse(self.settings.exists())
        self.run_cmd(install.cmd_install)
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(self.settings_data()["permissions"]["deny"], ["Read(~/.ssh/**)", "WebFetch"])

    def test_preview_happens_before_settings_write_and_twice_is_idempotent(self):
        self.choose("Bash(git push --force *)\nRead(~/.ssh/**)\n")
        write = Path.write_bytes
        with mock.patch("builtins.print") as printed:
            def checked_write(path, data):
                if path == self.settings:
                    preview = "\n".join(str(call.args[0]) for call in printed.call_args_list)
                    for rule in limits.load(self.file):
                        self.assertIn(f"{rule} — a deny rule cannot be lifted from inside a session", preview)
                return write(path, data)
            with mock.patch.object(Path, "write_bytes", checked_write):
                self.run_cmd(install.cmd_install)
        first = self.settings.read_bytes()
        manifest = (self.home / install.MANIFEST).read_bytes()
        self.run_cmd(install.cmd_install)
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertEqual((self.home / install.MANIFEST).read_bytes(), manifest)

    def test_uninstall_restores_preexisting_deny_rules_byte_for_byte(self):
        original = '{"permissions":{"deny":["Read(~/.ssh/**)","WebFetch"],"allow":["Read"]}}\n'
        self.put(original)
        self.choose("Read(~/.ssh/**)\nBash(git push --force *)\n")
        self.run_cmd(install.cmd_install)
        self.assertEqual(self.settings_data()["permissions"]["deny"],
                         ["Read(~/.ssh/**)", "WebFetch", "Bash(git push --force *)"])
        self.run_cmd(install.cmd_uninstall, dry=True)
        self.assertIn("Bash(git push --force *)", self.settings.read_text())
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(self.settings.read_bytes(), original.encode())
        self.assertTrue(self.file.exists())

    def test_user_edits_and_preexisting_empty_containers_survive_uninstall(self):
        for before in ({}, {"permissions": {}}, {"permissions": {"deny": []}}):
            with self.subTest(before=before):
                self.put(json.dumps(before))
                self.choose("Read(~/.ssh/**)\n")
                self.run_cmd(install.cmd_install)
                data = self.settings_data()
                data["user_edit"] = True
                self.put(json.dumps(data))
                self.run_cmd(install.cmd_uninstall)
                self.assertEqual(self.settings_data(), dict(before, user_edit=True))

    def test_uninstall_preserves_a_rule_the_user_added_after_install(self):
        self.choose("WebFetch\n")
        self.run_cmd(install.cmd_install)
        data = self.settings_data()
        data["permissions"]["deny"].extend(["Read(~/.ssh/**)", "WebFetch"])
        self.put(json.dumps(data))
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(self.settings_data(), {"permissions": {"deny": ["Read(~/.ssh/**)", "WebFetch"]}})

    def test_edited_settings_keep_preexisting_rules_even_if_limits_file_is_gone(self):
        self.put('{"permissions":{"deny":["WebFetch","Read(~/.ssh/**)"]}}')
        self.choose("WebFetch\nBash(git push --force *)\n")
        self.run_cmd(install.cmd_install)
        data = self.settings_data()
        data["theme"] = "dark"
        self.put(json.dumps(data))
        self.file.unlink()
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(self.settings_data(), {"permissions": {"deny": ["WebFetch", "Read(~/.ssh/**)"]}, "theme": "dark"})

    def test_reinstall_applies_list_edits_and_preserves_other_rules(self):
        self.put('{"permissions":{"deny":["WebFetch"]}}')
        self.choose("WebFetch\nRead(~/.ssh/**)\n")
        self.run_cmd(install.cmd_install)
        data = self.settings_data()
        data["permissions"]["deny"].append("Edit(~/diary/**)")
        data["permissions"]["allow"] = ["Read"]
        self.put(json.dumps(data))
        self.choose("Bash(git push --force *)\n")
        self.run_cmd(install.cmd_install)
        self.assertEqual(self.settings_data()["permissions"]["deny"],
                         ["WebFetch", "Edit(~/diary/**)", "Bash(git push --force *)"])
        self.choose("")
        self.run_cmd(install.cmd_install)
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(self.settings_data(), {"permissions": {"deny": ["WebFetch", "Edit(~/diary/**)"], "allow": ["Read"]}})

    def test_invalid_list_or_deny_settings_refuses_before_writing(self):
        for text, settings in (("never do harm\n", "{}"), ("Read\n", '{"permissions":{"deny":"Read"}}'),
                               ("Read\n", '{"permissions":null}')):
            with self.subTest(text=text, settings=settings):
                self.choose(text)
                self.put(settings)
                code, out = self.run_cmd(install.cmd_install)
                self.assertEqual(code, 2)
                self.assertIn("refused:", out)
                self.assertEqual(self.settings.read_text(), settings)
                self.assertFalse((self.home / install.MANIFEST).exists())

    def test_default_claude_directory_uses_scratch_home(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(install.claude_dir(), Path.home() / ".claude")
            self.choose("WebFetch\n")
            self.run_cmd(install.cmd_install)
            target = Path.home() / ".claude/settings.json"
            self.assertEqual(json.loads(target.read_text())["permissions"]["deny"], ["WebFetch"])
            self.run_cmd(install.cmd_uninstall)
            self.assertFalse(target.exists())


class MatcherTest(unittest.TestCase):
    def test_windows_paths_compare_case_insensitively(self):
        rule = "Read(C:/Users/u/.ssh/**)"
        call = {"file_path": r"c:\users\u\.ssh\id_rsa"}
        with mock.patch.object(limits, "_case_insensitive", return_value=True):
            self.assertTrue(limits.matches(rule, "Read", call))
        with mock.patch.object(limits, "_case_insensitive", return_value=False):
            self.assertFalse(limits.matches(rule, "Read", call))

    def test_a_rule_matches_any_statement_of_a_compound_command(self):
        for cmd in ("cd x && git push origin", "a || git push", "ls; git push -f", "cat f | git push", "x\ngit push"):
            self.assertTrue(limits.matches("Bash(git push:*)", "Bash", {"command": cmd}), cmd)
            self.assertTrue(limits.matches("PowerShell(git push:*)", "PowerShell", {"command": cmd}), cmd)

    def test_a_command_that_only_mentions_the_pattern_in_quotes_is_not_blocked(self):
        for cmd in ("echo 'cd x && git push origin'", 'echo "a; git push"', "git commit -m 'no git push | here'"):
            self.assertFalse(limits.matches("Bash(git push:*)", "Bash", {"command": cmd}), cmd)

    def test_plain_commands_match_as_before(self):
        self.assertTrue(limits.matches("Bash(git push:*)", "Bash", {"command": "git push origin"}))
        self.assertFalse(limits.matches("Bash(git push:*)", "Bash", {"command": "git status"}))

    def test_a_prefix_rule_matches_the_bare_command(self):
        self.assertTrue(limits.matches("Bash(git push:*)", "Bash", {"command": "git push"}))
        self.assertFalse(limits.matches("Bash(git push:*)", "Bash", {"command": "git pushx"}))
