"""The PreToolUse hook (commonroom#583). Every home is a scratch folder; nothing real is read or written."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import pretool, wire


class PreTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "room"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home), "HOME": self.tmp.name,
                                           "USERPROFILE": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("COMMONROOM_WORKER", None)

    def choose(self, text):
        (self.home / "limits.txt").write_text(text, encoding="utf-8", newline="\n")

    def call(self, tool, **tool_input):
        return pretool.hook(json.dumps({"tool_name": tool, "tool_input": tool_input}))

    def test_empty_or_missing_list_blocks_nothing(self):
        self.assertEqual(self.call("Bash", command="git push --force origin x"), (0, ""))
        self.choose("# Bash(git push --force *)\n\n")
        for tool, args in (("Bash", {"command": "git push --force origin x"}), ("Edit", {"file_path": "~/.ssh/x"}),
                           ("Bash", {"command": "taskkill /IM x.exe"})):
            self.assertEqual(self.call(tool, **args), (0, ""))

    def test_a_listed_rule_blocks_and_names_its_line_and_how_to_lift(self):
        self.choose("# mine\nBash(git push --force *)\nRead(~/.ssh/**)\n")
        code, msg = self.call("Bash", command="git push --force origin main")
        self.assertEqual(code, 2)
        self.assertIn("line 2", msg)
        self.assertIn("Bash(git push --force *)", msg)
        self.assertIn("delete that line", msg)
        code, msg = self.call("Read", file_path=str(Path.home() / ".ssh" / "id_rsa"))
        self.assertEqual(code, 2)
        self.assertIn("line 3", msg)

    def test_what_the_list_does_not_name_passes(self):
        self.choose("Bash(git push --force *)\nRead(~/.ssh/**)\n")
        for tool, args in (("Bash", {"command": "git push origin main"}), ("Read", {"file_path": "/tmp/x"}),
                           ("Edit", {"file_path": str(Path.home() / ".ssh" / "x")}),
                           ("Bash", {"command": "pkill -f something"})):   # not his list, so not blocked in his sessions
            self.assertEqual(self.call(tool, **args), (0, ""))

    def test_lifting_takes_effect_at_once(self):
        self.choose("Bash(rm -rf *)\n")
        self.assertEqual(self.call("Bash", command="rm -rf x")[0], 2)
        self.choose("")
        self.assertEqual(self.call("Bash", command="rm -rf x"), (0, ""))

    def test_errors_allow_and_print_nothing(self):
        self.choose("Bash(rm -rf *)\n")
        for text in ("not json", "", "[]", json.dumps({"tool_name": "Bash"}), json.dumps({"tool_input": 3})):
            self.assertEqual(pretool.hook(text), (0, ""))
        with mock.patch.object(pretool.limits, "first_match", side_effect=OSError("boom")):
            self.assertEqual(self.call("Bash", command="rm -rf x"), (0, ""))
        (self.home / "limits.txt").write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(self.call("Bash", command="rm -rf x"), (0, ""))

    def test_main_prints_the_reason_on_stderr_only(self):
        self.choose("Bash(rm -rf *)\n")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}))), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            self.assertEqual(pretool.main(), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("line 1", err.getvalue())

    def test_workers_get_the_command_guard_and_not_the_owners_list(self):
        self.choose("Bash(git push *)\n")
        with mock.patch.dict(os.environ, {"COMMONROOM_WORKER": "1"}):
            code, msg = self.call("Bash", command="pkill -f x")
            self.assertEqual(code, 2)
            self.assertIn("command guard", msg)
            self.assertEqual(self.call("Bash", command="git push origin x"), (0, ""))
            self.assertEqual(self.call("Bash", command="taskkill /PID 12 /F"), (0, ""))

    def test_owner_sessions_are_not_given_the_worker_guard(self):
        self.assertEqual(self.call("Bash", command="pkill -f x"), (0, ""))


class Wiring(unittest.TestCase):
    def test_plugin_registers_a_pre_tool_hook(self):
        data = json.loads((Path(wire.__file__).parent.parent / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))
        group = data["hooks"]["PreToolUse"][0]
        self.assertIn("Bash", group["matcher"])
        self.assertEqual(group["hooks"][0]["args"][-1], "pre-tool")

    def test_codex_gets_the_guard_on_its_shell_tool(self):
        with tempfile.TemporaryDirectory() as t:
            data, added, _ = wire.codex_plan(Path(t) / "hooks.json", Path(t) / "checkout")
            self.assertIn("PreToolUse", added)
            group = data["hooks"]["PreToolUse"][0]
            self.assertEqual(group["matcher"], "Bash")
            self.assertTrue(group["hooks"][0]["command"].endswith("run.py\" pre-tool"))


if __name__ == "__main__":
    unittest.main()
