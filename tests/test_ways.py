"""commonroom#216: the words workers are told live in commonroom/ways/prompts, not in dispatch.py. A project can replace
any of them from its own `[ways] dir`; without one, nothing changes (tests/test_prompt_golden.py shows it)."""
import ast
import fnmatch
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from commonroom import config, dispatch
from commonroom.ledger import Ledger

REPO = Path(__file__).resolve().parent.parent
PROMPT_DIR = REPO / "commonroom" / "ways" / "prompts"
# The code that builds what a worker is told. Its words belong in the templates.
PROMPT_METHODS = {"prompt", "continuation_prompt", "headless_note", "trust_note", "suite_note", "codex_note", "why",
                  "create_sweep_issue", "bulletin_missed"}
PROMPT_FUNCTIONS = {"steer_reason"}
PROMPT_CONSTANTS = {"HEADLESS_NOTE", "STEER_REASON", "BULLETIN_REASON"}
LONGEST = 80


def long_literals(node: ast.AST) -> list[str]:
    """String literals longer than LONGEST under `node` (an f-string counts its literal parts together), docstrings aside."""
    body = getattr(node, "body", None)
    skip = (body[0].value if isinstance(body, list) and body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant) else None)
    found, parts = [], set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.JoinedStr):
            literal = "".join(v.value for v in sub.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            parts.update(id(v) for v in sub.values)
            if len(literal) > LONGEST:
                found.append(literal)
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub is not skip and id(sub) not in parts
                and len(sub.value) > LONGEST):
            found.append(sub.value)
    for sub in ast.walk(node):   # text assembled with `+` from short literals counts as one
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Add):
            literal = "".join(literal_text(sub))
            if len(literal) > LONGEST and literal not in found:
                found.append(literal)
    return found


def literal_text(node: ast.AST) -> list[str]:
    """The literal string parts of a `+` chain, f-strings included."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal_text(node.left) + literal_text(node.right)
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    return [node.value] if isinstance(node, ast.Constant) and isinstance(node.value, str) else []


class AxiomA1(unittest.TestCase):
    def test_prompt_code_holds_no_long_text(self):
        tree = ast.parse((REPO / "commonroom" / "dispatch.py").read_text(encoding="utf-8"))
        seen = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "Dispatcher":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name in PROMPT_METHODS:
                        seen.add(item.name)
                        self.assertEqual(long_literals(item), [], item.name)
            elif isinstance(node, ast.FunctionDef) and node.name in PROMPT_FUNCTIONS:
                seen.add(node.name)
                self.assertEqual(long_literals(node), [], node.name)
            elif isinstance(node, ast.Assign) and {getattr(t, "id", None) for t in node.targets} & PROMPT_CONSTANTS:
                seen.add(node.targets[0].id)
                self.assertEqual(long_literals(node), [], node.targets[0].id)
        self.assertEqual(seen, PROMPT_METHODS | PROMPT_FUNCTIONS | PROMPT_CONSTANTS)

    def test_the_guard_catches_long_text(self):
        long = "x" * (LONGEST + 1)
        def found(source): return long_literals(ast.parse(source).body[0])
        self.assertEqual(len(found(f"def f(a):\n    return f'{long[:50]}{{a}}{long[50:]}'")), 1)
        self.assertEqual(len(found(f"def f():\n    return ('{long[:50]}' '{long[50:]}')")), 1)
        self.assertEqual(found(f"def f():\n    '''{long}'''\n    return 'short'"), [])
        self.assertEqual(len(found(f"def f(a):\n    return '{long[:50]}' + a + f'{{a}}{long[50:]}'")), 1)
        self.assertEqual(found("def f(a):\n    return '\\n\\n' + a + ' ' + a"), [])


class Templates(unittest.TestCase):
    def test_every_template_file_is_loaded_and_every_loaded_one_exists(self):
        self.assertEqual(sorted(p.stem for p in PROMPT_DIR.glob("*.md")), sorted(dispatch.PROMPTS))
        self.assertEqual(set(dispatch.WAYS), set(dispatch.PROMPTS))

    def test_templates_are_lf_and_name_no_project(self):
        for path in PROMPT_DIR.glob("*.md"):
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, path.name)
            self.assertTrue(raw.endswith(b"\n"), path.name)
            for word in (b"gamemirage", b"kgaydhani", b"Godot", b"D:/Dev"):
                self.assertNotIn(word.lower(), raw.lower(), path.name)

    def test_a_missing_template_fails_with_its_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "prompts").mkdir()
            with self.assertRaises(FileNotFoundError) as caught:
                dispatch.load_ways(Path(tmp))
            self.assertIn(f"{dispatch.PROMPTS[0]}.md", str(caught.exception))

    def test_fill_is_one_pass_and_leaves_other_braces_alone(self):
        self.assertEqual(dispatch.fill_way("`{a}` <b> {{x}}!", x="{{x}}"), "`{a}` <b> {{x}}!")
        with self.assertRaises(KeyError):
            dispatch.fill_way("{{missing}}")

    def test_loaded_from_the_module_folder_whatever_the_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = (f"import sys; sys.path.insert(0, {str(REPO)!r}); from commonroom import dispatch; "
                    f"print(len(dispatch.WAYS))")
            out = subprocess.run([sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), str(len(dispatch.PROMPTS)), out.stderr)

    def test_the_templates_ship_with_the_package(self):
        with open(REPO / "pyproject.toml", "rb") as f:
            globs = tomllib.load(f)["tool"]["setuptools"]["package-data"]["commonroom"]
        for path in PROMPT_DIR.glob("*.md"):
            name = path.relative_to(REPO / "commonroom").as_posix()
            self.assertTrue(any(fnmatch.fnmatch(name, g) for g in globs), name)


class Board:
    def pr_for_branch(self, branch): return None


class ProjectOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, extra=None):
        cfg = config.merge(config.DEFAULTS, {"project": {"name": "p", "repo": "o/p"}, "goal": {"milestone": "M"},
                                             **(extra or {})})
        cfg["_root"] = self.tmp.name
        return dispatch.Dispatcher(cfg, self.ledger, board=Board(), log=lambda *_: None)

    def write(self, name, text):
        path = self.root / "team-ways" / "prompts" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def jobs(self):
        return [dispatch.Job("build", "builder", issue={"number": 7, "title": "t", "labels": [], "comments": []}),
                dispatch.Job("role", "playtester", issue={"number": 7, "title": "t", "labels": [], "comments": []}),
                dispatch.Job("direct", "director", reason="x")]

    def test_unset_means_the_room_templates(self):
        self.assertIs(self.dispatcher().ways, dispatch.WAYS)
        self.assertIs(self.dispatcher({"ways": {}}).ways, dispatch.WAYS)

    def test_a_project_file_replaces_only_its_namesake(self):
        self.write("build", "Build #{{n}} in {{repo}} on `{{branch}}`.{{suite_note}}{{open_pr}}\r\n")
        self.write("steer", "steer the project")
        plain, custom = self.dispatcher(), self.dispatcher({"ways": {"dir": "team-ways"}})
        texts = [(d.prompt(job, "i7-t"), d.headless_note()) for d in (plain, custom) for job in self.jobs()]
        self.assertEqual(texts[3], ("Build #7 in o/p on `i7-t`.", plain.headless_note()))
        self.assertEqual(texts[1:3], texts[4:6])   # the role and director prompts are the room's
        self.assertEqual(dispatch.steer_reason({"director": {"log_issue": 9}}, custom.ways), "steer the project (#9)")
        self.assertEqual(dispatch.steer_reason({}, plain.ways), dispatch.STEER_REASON)

    def test_an_override_may_only_use_the_rooms_placeholders(self):
        self.write("role", "Do #{{n}} as {{role}} for {{owner}}")
        with self.assertRaises(ValueError) as caught:
            self.dispatcher({"ways": {"dir": "team-ways"}})
        self.assertIn("role.md", str(caught.exception))
        self.assertIn("owner", str(caught.exception))

    def test_a_missing_ways_folder_fails_at_start(self):
        with self.assertRaises(FileNotFoundError) as caught:
            self.dispatcher({"ways": {"dir": "no-such-folder"}})
        self.assertIn("no-such-folder", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
