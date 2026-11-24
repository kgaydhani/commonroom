import re
import shlex
import tempfile
import unittest
from pathlib import Path

from commonroom import cli, config as config_mod, starter

OWNER_LOGIN = "kgaydhani"


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "demo"
        self.root.mkdir()
        self.lines = []

    def init(self, goal=None):
        return starter.run(str(self.root), goal, out=self.lines.append)

    def test_writes_loadable_toml_and_every_role_file(self):
        self.assertEqual(self.init("Ship v1"), 0)
        cfg = config_mod.load(self.root / "room.toml")
        self.assertEqual(cfg["project"]["name"], "demo")
        self.assertEqual(cfg["goal"]["milestone"], "Ship v1")
        self.assertTrue(cfg["roles"])
        for role in cfg["roles"]:
            self.assertTrue((self.root / ".claude" / "agents" / f"{role}.md").is_file(), role)

    def test_existing_file_means_nothing_written(self):
        (self.root / ".claude" / "agents").mkdir(parents=True)
        mine = self.root / ".claude" / "agents" / "reviewer.md"
        mine.write_text("mine", encoding="utf-8")
        self.assertNotEqual(self.init(), 0)
        self.assertEqual(mine.read_text(encoding="utf-8"), "mine")
        self.assertFalse((self.root / "room.toml").exists())
        self.assertFalse((self.root / ".claude" / "agents" / "builder.md").exists())
        self.assertTrue(any("reviewer.md" in line for line in self.lines))

    def test_templates_hold_no_login_or_drive_path(self):
        for f in starter.TEMPLATES.iterdir():
            text = f.read_text(encoding="utf-8")
            self.assertNotIn(OWNER_LOGIN, text.lower(), f.name)
            self.assertIsNone(re.search(r"\b[A-Za-z]:[/\\]", text), f.name)

    def test_next_step_is_a_projects_add_the_parser_accepts(self):
        self.init()
        line = next(l.strip() for l in self.lines if l.strip().startswith("room "))
        argv = shlex.split(line)[1:]
        self.assertEqual(argv[:2], ["projects", "add"])
        args = cli.build_parser().parse_args(argv)
        self.assertEqual((args.action, args.name, args.path), ("add", "demo", str(self.root)))

    def test_not_a_folder_is_refused(self):
        self.assertNotEqual(starter.run(str(self.root / "nope"), out=self.lines.append), 0)


if __name__ == "__main__":
    unittest.main()
