"""Every `room.py <command>` the README names must be a subcommand of the CLI."""
import contextlib
import io
import re
import unittest
from pathlib import Path

from commonroom import cli

README = Path(__file__).resolve().parent.parent / "README.md"
COMMAND = re.compile(r"\broom(?:\.py)?[ \t]+([a-z][a-z-]*)")
CODE = re.compile(r"```.*?```|`[^`\n]+`", re.S)


def cli_commands() -> set[str]:
    """The subcommand names, read from argparse's own 'invalid choice' error."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            cli.main(["no-such-command-"])
        except SystemExit:
            pass
    m = re.search(r"choose from (.*?)\)?\s*$", err.getvalue().strip().splitlines()[-1])
    return {c.strip().strip("'\"") for c in m.group(1).rstrip(")").split(",")}


def code_of(text: str) -> str:
    """Only code (fenced blocks and inline spans), so prose like "the room builds" is ignored."""
    return "\n".join(CODE.findall(text))


def missing(text: str) -> list[str]:
    known = cli_commands()
    return sorted({c for c in COMMAND.findall(code_of(text)) if c not in known})


class ReadmeCommands(unittest.TestCase):
    def test_parser_commands_found(self):
        self.assertIn("projects", cli_commands())
        self.assertIn("beat", cli_commands())

    def test_every_command_in_readme_exists(self):
        text = README.read_text(encoding="utf-8")
        self.assertGreater(len(COMMAND.findall(code_of(text))), 8)
        self.assertEqual(missing(text), [], "README names commands the CLI lacks")

    def test_made_up_command_is_caught(self):
        text = README.read_text(encoding="utf-8") + "\n```\npython D:/Dev/commonroom/room.py frobnicate\n```\n"
        self.assertEqual(missing(text), ["frobnicate"])

    def test_no_scheduled_task_switching(self):
        self.assertNotIn("pointing the scheduled task", README.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
