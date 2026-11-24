import os
import tempfile
import unittest
from pathlib import Path

from commonroom import doctor

TOML = '[project]\nname = "{n}"\n[roles]\nbuilder = ["{p}:sonnet"]\n'


def tree(root: Path):
    return sorted(str(p) for p in root.rglob("*"))


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.reg = self.home / "projects.toml"
        self.lines = []

    def project(self, name, provider="claude", toml=True, agent=True):
        root = self.home / name
        root.mkdir()
        if toml:
            (root / "room.toml").write_text(TOML.format(n=name, p=provider), encoding="utf-8")
        if agent:
            (root / ".claude" / "agents").mkdir(parents=True)
            (root / ".claude" / "agents" / "builder.md").write_text("---\nname: builder\n---\nx", encoding="utf-8")
        return root

    def registry(self, *projects, share=10):
        text = "".join(f'[projects.{r.name}]\npath = "{r.as_posix()}"\nshare = {share}\n' for r in projects)
        self.reg.write_text(text, encoding="utf-8")

    def run_doctor(self, tools=("claude", "gh", "codex"), gh_code=0):
        code = doctor.run(self.reg, which=lambda n: n if n in tools else None, gh=lambda a: gh_code,
                          version=(3, 12), out=self.lines.append)
        return code

    def test_all_present(self):
        self.registry(self.project("alpha"))
        before = tree(self.home)
        code = self.run_doctor(tools=("claude", "gh"))
        self.assertEqual(code, 0, self.lines)
        self.assertTrue(all(l.endswith(": ok") for l in self.lines), self.lines)
        self.assertEqual(tree(self.home), before)   # wrote nothing

    def test_missing_gh(self):
        self.registry(self.project("alpha"))
        self.assertEqual(self.run_doctor(tools=("claude",)), 1)
        self.assertTrue(any("gh on PATH: missing - install the GitHub CLI" in l for l in self.lines))

    def test_gh_not_logged_in(self):
        self.registry(self.project("alpha"))
        self.assertEqual(self.run_doctor(tools=("claude", "gh"), gh_code=1), 1)
        self.assertTrue(any("gh auth login" in l for l in self.lines))

    def test_registry_over_100(self):
        self.registry(self.project("alpha"), self.project("beta"), share=60)
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("registry valid: missing" in l and "more than 100" in l for l in self.lines))

    def test_project_without_room_toml(self):
        self.registry(self.project("alpha", toml=False, agent=False))
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("project alpha room.toml: missing - add a room.toml" in l for l in self.lines))

    def test_role_without_agent_file(self):
        self.registry(self.project("alpha", agent=False))
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("alpha roles: missing" in l and "builder.md" in l for l in self.lines))

    def test_codex_only_needed_when_named(self):
        self.registry(self.project("alpha"))
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 0)
        self.assertFalse(any("codex" in l for l in self.lines))
        self.lines.clear()
        self.reg.unlink()
        self.registry(self.project("beta", provider="codex"))
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("codex on PATH: missing" in l for l in self.lines))

    def test_old_python(self):
        self.registry(self.project("alpha"))
        code = doctor.run(self.reg, which=lambda n: n, gh=lambda a: 0, version=(3, 10), out=self.lines.append)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
