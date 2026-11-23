import tomllib
import unittest
from pathlib import Path

from commonroom import dispatch

ROOT = Path(__file__).resolve().parent.parent
OLD, NEW, HEAD = "python -m unittest discover -s tests", "python tests/parallel.py", "abc1234def"


def line(command):
    return f"[agent:reviewer] REVIEW: APPROVE\nTESTS: PASS {HEAD[:7]} {command}: Ran 3 tests OK"


class ChecksCommand(unittest.TestCase):
    def test_room_toml_runs_the_parallel_runner(self):
        cfg = tomllib.loads((ROOT / "room.toml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["checks"]["test"], NEW)
        self.assertEqual(dispatch.also_accept(cfg), [OLD])

    def test_old_command_accepted_only_when_listed(self):
        self.assertFalse(dispatch.tested_on_head(line(OLD), HEAD, NEW))
        self.assertFalse(dispatch.tested_on_head(line(OLD), HEAD, NEW, dispatch.also_accept({"checks": {}})))
        self.assertTrue(dispatch.tested_on_head(line(OLD), HEAD, NEW, dispatch.also_accept({"checks": {"also_accept": [OLD]}})))
        self.assertTrue(dispatch.tested_on_head(line(NEW), HEAD, NEW, [OLD]))

    def test_listed_old_command_does_not_excuse_a_fail_or_wrong_sha(self):
        also = [OLD]
        self.assertFalse(dispatch.tested_on_head(line(OLD) + "\nTESTS: FAIL abc1234 x", HEAD, NEW, also))
        self.assertFalse(dispatch.tested_on_head(line(OLD).replace("abc1234", "fedcba9"), HEAD, NEW, also))
        self.assertFalse(dispatch.tested_on_head(line("pytest"), HEAD, NEW, also))

    def test_no_setting_behaves_as_before(self):
        self.assertEqual(dispatch.also_accept({}), [])
        self.assertTrue(dispatch.tested_on_head(line(OLD), HEAD, OLD))
