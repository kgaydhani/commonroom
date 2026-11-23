"""`room config show|set` (commonroom#451). A temporary room home and project folder; never the live room."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, registry
from commonroom.registry import Project, Registry

SAMPLE = (
    "# my project\n"
    "[project]\n"
    'name = "demo"  # the name\n'
    "\n"
    "# how often the director steers\n"
    "[director]\n"
    "steer_every_hours = 24   # keep it daily\n"
    "\n"
    "[budget.claude]\n"
    "reserve_weekly = 25\n"
    "limits = { a = 1 }\n"
    "list = [\n  1,\n  2,\n]\n"
    "\n"
    "[goal]\n"
    'milestone = "M"\n'
)


class ConfigCommand(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(root / "home")})
        env.start()
        self.addCleanup(env.stop)
        folder = root / "demo"
        folder.mkdir()
        self.file = folder / "room.toml"
        self.file.write_bytes(SAMPLE.encode())
        registry.save(Registry({"demo": Project("demo", str(folder), 50)}))

    def room(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["config", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_show_lists_keys_and_one_key(self):
        code, out, _ = self.room("show", "demo")
        self.assertEqual(code, 0)
        self.assertIn("director.steer_every_hours = 24\n", out)
        self.assertIn('project.name = "demo"\n', out)
        self.assertEqual(self.room("show", "demo", "director.steer_every_hours")[1], "director.steer_every_hours = 24\n")
        self.assertEqual(self.room("show", "demo", "nope.x")[0], 2)

    def test_set_without_yes_prints_diff_and_writes_nothing(self):
        code, out, _ = self.room("set", "demo", "director.steer_every_hours", "12")
        self.assertEqual(code, 0)
        self.assertIn("-steer_every_hours = 24", out)
        self.assertIn("+steer_every_hours = 12   # keep it daily", out)
        self.assertEqual(self.file.read_bytes(), SAMPLE.encode())

    def test_set_with_yes_changes_only_that_line(self):
        self.assertEqual(self.room("set", "demo", "director.steer_every_hours", "12", "--yes")[0], 0)
        after = self.file.read_bytes().decode()
        self.assertEqual(after, SAMPLE.replace("= 24 ", "= 12 "))
        self.assertEqual(self.room("set", "demo", "goal.milestone", '"N"', "--yes")[0], 0)
        self.assertIn('milestone = "N"\n', self.file.read_text())

    def test_new_key_lands_at_end_of_its_table_and_new_table_at_end(self):
        self.assertEqual(self.room("set", "demo", "director.mode", '"calm"', "--yes")[0], 0)
        after = self.file.read_bytes().decode()
        self.assertIn('steer_every_hours = 24   # keep it daily\nmode = "calm"\n\n[budget.claude]', after)
        self.assertEqual(self.room("set", "demo", "parallel.claude_slots", "1", "--yes")[0], 0)
        self.assertTrue(self.file.read_text().endswith('milestone = "M"\n\n[parallel]\nclaude_slots = 1\n'))
        self.assertEqual(self.room("set", "demo", "budget.claude.pace", "false", "--yes")[0], 0)
        self.assertIn("pace = false\n", self.file.read_text())

    def test_refusals_exit_2_and_leave_the_file(self):
        for argv in (
            ("demo", "director.steer_every_hours", "not toml"),
            ("nobody", "director.steer_every_hours", "1"),
            ("demo", "budget.claude.limits", "3"),
            ("demo", "budget.claude.list", "3"),
            ("demo", "budget.claude", "3"),
            ("demo", "goal.approve", '"bogus"'),
            ("demo", "nodot", "1"),
        ):
            code, _, err = self.room("set", *argv, "--yes")
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.startswith("refused: ") and err.count("\n") == 1, (argv, err))
            self.assertEqual(self.file.read_bytes(), SAMPLE.encode(), argv)

    def test_crlf_file_keeps_crlf(self):
        crlf = SAMPLE.replace("\n", "\r\n").encode()
        self.file.write_bytes(crlf)
        self.assertEqual(self.room("set", "demo", "director.mode", "1", "--yes")[0], 0)
        self.assertEqual(self.file.read_bytes().decode().count("\n"), SAMPLE.count("\n") + 1)
        self.assertNotIn(b"\n", self.file.read_bytes().replace(b"\r\n", b""))
