"""`room install` / `room uninstall` (commonroom#566): scratch room home and scratch Claude config dir only."""
import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from commonroom import install

OTHER = ('{\n    "model": "opus",\n    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]},\n'
         '    "enabledPlugins": {"other@m": true},\n    "zeta": 1,\n    "alpha": 2\n}\n')


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home, self.claude = t / "home", t / "claude"
        self.settings = self.claude / "settings.json"
        old = {k: os.environ.get(k) for k in ("COMMONROOM_HOME", "CLAUDE_CONFIG_DIR")}
        os.environ["COMMONROOM_HOME"], os.environ["CLAUDE_CONFIG_DIR"] = str(self.home), str(self.claude)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        self.addCleanup(self.tmp.cleanup)

    def run_cmd(self, fn, dry=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(argparse.Namespace(dry_run=dry))
        return code, out.getvalue()

    def put(self, text):
        self.claude.mkdir(parents=True, exist_ok=True)
        self.settings.write_bytes(text.encode())

    def test_adds_plugin_and_status_line_and_keeps_other_content(self):
        self.put(OTHER)
        code, out = self.run_cmd(install.cmd_install)
        self.assertEqual(code, 0)
        data = json.loads(self.settings.read_text())
        before = json.loads(OTHER)
        for k, v in before.items():
            if k != "enabledPlugins":
                self.assertEqual(data[k], v)
        self.assertEqual(data["enabledPlugins"], {"other@m": True, "commonroom@commonroom": True})
        self.assertIn("room.py tap", data["statusLine"]["command"])
        self.assertEqual(data["extraKnownMarketplaces"]["commonroom"]["source"]["source"], "directory")
        self.assertEqual(list(data)[:5], list(before))          # existing key order survives
        self.assertIn("    ", self.settings.read_text())        # indent kept
        self.assertEqual((self.home / "room-path").read_text().strip(), (install.checkout() / "room.py").as_posix())
        self.assertIn("add    statusLine", out)

    def test_dry_run_prints_and_writes_nothing(self):
        self.put(OTHER)
        code, out = self.run_cmd(install.cmd_install, dry=True)
        self.assertEqual(self.settings.read_bytes(), OTHER.encode())
        self.assertFalse(self.home.exists())
        self.assertIn("add    enabledPlugins.commonroom@commonroom", out)
        self.assertIn("Dry run", out)

    def test_twice_makes_no_second_change(self):
        self.put(OTHER)
        self.run_cmd(install.cmd_install)
        first = self.settings.read_bytes()
        manifest = (self.home / "install.json").read_bytes()
        code, out = self.run_cmd(install.cmd_install)
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertEqual((self.home / "install.json").read_bytes(), manifest)
        self.assertIn("Nothing to change", out)

    def test_round_trip_restores_bytes(self):
        for original in (OTHER, "{}", OTHER.replace("\n", "\r\n"), None):
            with self.subTest(original=original):
                if original is None:
                    self.settings.unlink(missing_ok=True)
                else:
                    self.put(original)
                self.run_cmd(install.cmd_install)
                self.run_cmd(install.cmd_uninstall)
                if original is None:
                    self.assertFalse(self.settings.exists())
                else:
                    self.assertEqual(self.settings.read_bytes(), original.encode())
                self.assertFalse((self.home / "room-path").exists())
                self.assertFalse((self.home / "install.json").exists())

    def test_uninstall_after_the_user_edited_removes_only_ours(self):
        self.put(OTHER)
        self.run_cmd(install.cmd_install)
        data = json.loads(self.settings.read_text())
        data["newkey"] = 3
        self.settings.write_text(json.dumps(data, indent=4))
        self.run_cmd(install.cmd_uninstall)
        after = json.loads(self.settings.read_text())
        want = json.loads(OTHER)
        want["newkey"] = 3
        self.assertEqual(after, want)

    def test_existing_status_line_is_left_alone(self):
        self.put('{"statusLine": {"type": "command", "command": "mine"}}\n')
        _, out = self.run_cmd(install.cmd_install)
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"]["command"], "mine")
        self.assertIn("skip", out)
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(self.settings.read_text(), '{"statusLine": {"type": "command", "command": "mine"}}\n')

    def test_uninstall_when_not_installed(self):
        code, out = self.run_cmd(install.cmd_uninstall)
        self.assertEqual(code, 0)
        self.assertIn("not installed", out)

    def test_bad_json_is_refused_and_untouched(self):
        self.put("{nope")
        code, _ = self.run_cmd(install.cmd_install)
        self.assertEqual(code, 2)
        self.assertEqual(self.settings.read_text(), "{nope")


if __name__ == "__main__":
    unittest.main()
