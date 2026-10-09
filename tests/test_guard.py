import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, dispatch, guard, workers

ROOT = Path(__file__).resolve().parent.parent

BLOCKED = [
    'taskkill //F //FI "WINDOWTITLE eq *"',            # the command from run b437ef897726
    'taskkill /F /IM chrome.exe',
    'TASKKILL /im python.exe /t',
    'taskkill -fi "IMAGENAME eq node.exe"',
    'C:/Windows/System32/taskkill.exe //IM chrome.exe //F',
    'cd /tmp && taskkill /F /IM serve.exe',
    'cmd /c taskkill /IM chrome.exe',
    'powershell -Command "Stop-Process -Name chrome -Force"',
    'Stop-Process -Name python',
    'stop-process -n chrome',
    'spps -Name chrome',
    'kill -Name chrome',
    'Get-Process chrome | Stop-Process',
    'gps python | Stop-Process -Force',
    'Get-Process chrome | % { Stop-Process $_ }',
    'Get-Process | Where-Object { $_.MainWindowTitle } | ForEach-Object { $_.Kill() }',
    'Stop-Process -Id (Get-Process chrome).Id',
    'ps aux | grep serve.py | awk \'{print $2}\' | xargs kill -9',
    'kill $(pgrep -f serve.py)',
    'pkill -f serve.py',
    'sudo killall chrome',
    'echo done; pkill python',
]

ALLOWED = [
    'taskkill /PID 1234 /F',
    'taskkill //PID 1234 //F //T',
    'Stop-Process -Id 1234',
    'Stop-Process -Id 1234 -Force',
    'kill 1234',
    'kill -9 1234 5678',
    'kill -n 9 1234',                                  # bash: -n is a signal, not a name
    'git commit -m "Stop workers using taskkill /IM chrome.exe"',
    'git log --grep "pkill"',
    'grep -rn "Stop-Process -Name" docs/',
    'Get-Process chrome',
    'ps aux | grep serve.py',
    'python -m unittest discover -s tests',
    'echo skill /IM',
]


class GuardMatcherTests(unittest.TestCase):
    def test_blocks_filter_based_kills(self):
        for command in BLOCKED:
            with self.subTest(command=command):
                self.assertIsNotNone(guard.blocked(command))

    def test_allows_kills_by_pid_and_plain_mentions(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(guard.blocked(command))

    def test_quoted_text_is_a_command_only_after_a_launcher(self):
        self.assertIsNone(guard.blocked('git commit -m "taskkill /IM chrome.exe was the bug"'))
        self.assertIsNotNone(guard.blocked("bash -c 'pkill -f serve.py'"))
        self.assertIsNotNone(guard.blocked('pwsh -NoProfile -Command "Get-Process chrome | Stop-Process"'))
        # After a separator inside quotes the text reads as a command: a false block costs a rephrase.
        self.assertIsNotNone(guard.blocked('git commit -m "fix; pkill python"'))


class GuardHookTests(unittest.TestCase):
    def hook(self, payload):
        return guard.hook(json.dumps(payload))

    def test_blocks_bash_and_powershell_with_exit_2_and_a_reason(self):
        for tool in ("Bash", "PowerShell"):
            code, message = self.hook({"tool_name": tool, "tool_input": {"command": "Get-Process chrome | Stop-Process"}})
            self.assertEqual(code, 2)
            self.assertIn("by PID", message)

    def test_lets_other_tools_and_bad_input_through(self):
        self.assertEqual(self.hook({"tool_name": "Bash", "tool_input": {"command": "taskkill /PID 1 /F"}}), (0, ""))
        self.assertEqual(self.hook({"tool_name": "Write", "tool_input": {"content": "pkill x"}}), (0, ""))
        self.assertEqual(guard.hook("not json"), (0, ""))
        self.assertEqual(guard.hook(""), (0, ""))

    def test_cli_entry_point_reads_stdin(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "pkill -f serve.py"}})
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["guard-command"]), 2)
        self.assertIn("command guard", err.getvalue())

    def test_room_py_hook_process(self):
        # The exact command line the hook runs, in a fresh interpreter.
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": 'taskkill //F //FI "WINDOWTITLE eq *"'}})
        done = subprocess.run([sys.executable, str(ROOT / "room.py"), "guard-command"], input=payload,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn("taskkill", done.stderr)


class ClaudeCommandTests(unittest.TestCase):
    def test_every_claude_worker_gets_the_guard_hook(self):
        for name in ("builder", "art-tech"):   # game and room roles alike
            role = workers.Role(name, None, [], "prompt")
            cmd = workers.claude_command(role, None, "sid", Path("p.md"), None)
            settings = json.loads(cmd[cmd.index("--settings") + 1])
            (entry,) = settings["hooks"]["PreToolUse"]
            self.assertEqual(entry["matcher"], "Bash|PowerShell")
            command = entry["hooks"][0]["command"]
            self.assertIn("room.py", command)
            self.assertTrue(command.endswith("guard-command"))
            # Only hooks: --settings adds to the user's and the project's settings, it must not override anything else.
            self.assertEqual(list(settings), ["hooks"])

    def test_headless_note_says_stop_only_your_own_processes(self):
        self.assertIn("Stop only processes you started, by PID.", dispatch.HEADLESS_NOTE)


if __name__ == "__main__":
    unittest.main()
