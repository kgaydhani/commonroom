import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from commonroom import cli, config as config_mod, registry, starter

OWNER_LOGIN = "kgaydhani"


def split(line):
    """Split as the platform's shell would: cmd quoting on Windows, POSIX elsewhere."""
    if sys.platform == "win32":
        return [a.strip('"') for a in shlex.split(line, posix=False)]
    return shlex.split(line)


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
        for f in (f for f in starter.TEMPLATES.rglob("*") if f.is_file()):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn(OWNER_LOGIN, text.lower(), f.name)
            self.assertIsNone(re.search(r"\b[A-Za-z]:[/\\]", text), f.name)

    def test_next_step_is_a_projects_add_the_parser_accepts(self):
        self.init()
        line = next(l.strip() for l in self.lines if l.strip().startswith("room "))
        argv = split(line)[1:]
        self.assertEqual(argv[:2], ["projects", "add"])
        args = cli.build_parser().parse_args(argv)
        self.assertEqual((args.action, args.name, args.path), ("add", "demo", str(self.root)))

    def test_awkward_folder_name_gives_a_registry_name_and_a_working_command(self):
        self.root = Path(self.tmp.name) / "My Proj.v2"
        self.root.mkdir()
        self.assertEqual(self.init(), 0)
        self.assertEqual(config_mod.load(self.root / "room.toml")["project"]["name"], "My-Proj-v2")
        line = next(l.strip() for l in self.lines if l.strip().startswith("room "))
        argv = split(line)[1:]
        args = cli.build_parser().parse_args(argv)
        self.assertEqual((args.name, args.path), ("My-Proj-v2", str(self.root)))
        self.assertRegex(args.name, registry.NAME)

    def test_goal_beyond_the_basic_plane_still_loads(self):
        self.assertEqual(self.init("Launch \U0001F680"), 0)
        self.assertEqual(config_mod.load(self.root / "room.toml")["goal"]["milestone"], "Launch \U0001F680")

    def test_goal_toml_cannot_hold_writes_nothing(self):
        self.assertNotEqual(self.init("bad\x7fgoal"), 0)
        for t in starter.targets(self.root):
            self.assertFalse(t.exists(), t)

    def test_not_a_folder_is_refused(self):
        self.assertNotEqual(starter.run(str(self.root / "nope"), out=self.lines.append), 0)


class ResearchStarterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "demo"
        self.root.mkdir()
        self.lines = []

    def init(self, goal=None, kind="research"):
        return starter.run(str(self.root), goal, out=self.lines.append, kind=kind)

    def written(self):
        self.assertEqual(self.init("Find things"), 0)
        agents = self.root / ".claude" / "agents"
        return {p.name: p.read_text(encoding="utf-8") for p in agents.iterdir()}

    def test_writes_a_local_board_project_whose_roles_have_files(self):
        self.written()
        cfg = config_mod.load(self.root / "room.toml")
        self.assertEqual(cfg["project"]["board"], "local")
        self.assertIsNone(cfg["project"]["repo"])
        self.assertEqual(set(cfg["roles"]), {"director", "researcher"})
        for role in cfg["roles"]:
            self.assertTrue((self.root / ".claude" / "agents" / f"{role}.md").is_file(), role)

    def test_never_overwrites(self):
        (self.root / "room.toml").write_text("mine", encoding="utf-8")
        self.assertNotEqual(self.init(), 0)
        self.assertEqual((self.root / "room.toml").read_text(encoding="utf-8"), "mine")
        self.assertFalse((self.root / ".claude").exists())

    def test_an_unknown_kind_writes_nothing(self):
        self.assertNotEqual(self.init(kind="nope"), 0)
        self.assertFalse((self.root / "room.toml").exists())

    def test_the_files_carry_each_point(self):
        files = self.written()
        both = (files["researcher.md"] + files["director.md"]).lower()
        for words in ("one step", "recheck", "seeds", "effort", "payoff", "cheapest probe", "for good", "anywhere",
                      "trial", "money", "account", "public", "only the owner can run", "room note", "method",
                      "protocol", "registers", "read and follow", "room add", "status: done"):
            self.assertIn(words, both, words)
        self.assertIn("seeds", files["director.md"].lower())
        self.assertIn("only to making seeds", files["director.md"].lower())

    def test_the_files_name_no_idea_project_person_or_domain(self):
        text = "\n".join(self.written().values()) + (starter.TEMPLATES / "research" / "room.toml").read_text(encoding="utf-8")
        for word in ("gamemirage", "magazine", "kieran", "kgaydhani", "commonroom", "stock", "crypto", "trading",
                     "newsletter", "etsy", "youtube", "game", "bitcoin"):
            self.assertNotIn(word, text.lower(), word)
        self.assertIsNone(re.search(r"\b[A-Za-z]:[/\\]", text))

    def test_the_default_kind_writes_what_it_always_wrote(self):
        self.assertEqual(self.init(kind="code"), 0)
        got = {t.relative_to(self.root).as_posix(): t.read_text(encoding="utf-8") for t in starter.targets(self.root)}
        self.assertEqual(set(got), {"room.toml", ".claude/agents/builder.md", ".claude/agents/reviewer.md",
                                    ".claude/agents/director.md"})
        for name, text in got.items():
            src = starter.TEMPLATES / Path(name).name
            expect = src.read_text(encoding="utf-8")
            if name == "room.toml":
                expect = expect.replace("{name}", '"demo"').replace("{goal}", '"First milestone"')
            self.assertEqual(text, expect, name)

    def test_the_cli_takes_kind_and_defaults_to_code(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["init", "x"]).kind, "code")
        self.assertEqual(parser.parse_args(["init", "x", "--kind", "research"]).kind, "research")


if __name__ == "__main__":
    unittest.main()
