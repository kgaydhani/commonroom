"""Owner-chosen limits only; all homes, settings, projects and ledgers are scratch."""
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from commonroom import install, ledger, limits
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
