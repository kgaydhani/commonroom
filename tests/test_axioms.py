"""The axioms (docs/axioms.md) and the decisions ledger that ties each one to a test (docs/decisions.csv, commonroom#215).

The ledger test checks the ledger's shape and that every check it names exists. The suite runs those checks, so an
`active` row whose test fails fails the run. A2 and A6 have their checks here; A4 and A5 point at existing tests."""
import ast
import collections
import csv
import datetime
import re
import subprocess
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dispatch, usage, workers
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with, issue

ROOT = Path(__file__).resolve().parent.parent
LEDGER = Path("docs/decisions.csv")
AXIOMS = Path("docs/axioms.md")
COLUMNS = ["id", "decision", "axiom", "source", "date", "check", "status"]
CHECK = re.compile(r"(tests/test_\w+\.py)::(\w+)::(test_\w+)")
SUPERSEDED = re.compile(r"superseded: ([A-Z][A-Z0-9-]*)")
MISSING = re.compile(r"Missing: \S")


def axiom_names(text: str) -> set[str]:
    """What a row's `axiom` may name: a row of the axioms table (`| A2 |`) or a heading."""
    names = {m[1] for m in re.finditer(r"^\|\s*([A-Z]\d+)\s*\|", text, re.M)}
    return names | {m[1].strip() for m in re.finditer(r"^#+\s+(.+)$", text, re.M)}


def check_problem(root: Path, check: str) -> str | None:
    """Why `check` doesn't name a test that exists, or None if it does."""
    match = CHECK.fullmatch(check)
    if not match:
        return "check must be tests/test_<file>.py::<TestClass>::test_<name>"
    path = root / match[1]
    if not path.is_file():
        return f"check file does not exist: {match[1]}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == match[2]), None)
    if cls is None:
        return f"test class does not exist: {match[1]}::{match[2]}"
    if not any(isinstance(n, ast.FunctionDef) and n.name == match[3] for n in cls.body):
        return f"test does not exist: {check}"
    return None


def validate(root: Path = ROOT, path: Path = LEDGER, axioms: Path = AXIOMS) -> tuple[list[dict], list[str]]:
    """Return (rows, errors). Each error names the row and what is wrong with it."""
    try:
        names = axiom_names((root / axioms).read_text(encoding="utf-8"))
        with (root / path).open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            if reader.fieldnames != COLUMNS:
                return [], [f"{path}: header must be {','.join(COLUMNS)}"]
            rows = list(reader)
    except (OSError, csv.Error, UnicodeError) as exc:
        return [], [f"{path}: {exc}"]
    errors = [] if rows else [f"{path}: the ledger has no rows"]
    ids = collections.Counter(row.get("id") for row in rows)
    for row in rows:
        def fail(reason):
            errors.append(f"[{row.get('id') or 'no id'}] {reason}")

        if None in row or any(row.get(key) is None for key in COLUMNS):
            fail(f"every row needs exactly {len(COLUMNS)} columns")
            continue
        if any(row[key] != row[key].strip() for key in COLUMNS):
            fail("remove leading or trailing spaces")
        if any(not row[key] for key in COLUMNS if key != "check"):
            fail("only `check` may be empty")
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9-]*", row["id"]):
            fail("id must be uppercase letters, digits and hyphens")
        if ids[row["id"]] > 1:
            fail("duplicate id")
        if row["axiom"] not in names:
            fail(f"axiom {row['axiom']!r} is not a row or heading in {axioms}")
        try:
            if datetime.date.fromisoformat(row["date"]).isoformat() != row["date"]:
                raise ValueError
        except ValueError:
            fail("date must be YYYY-MM-DD")
        status, check = row["status"], row["check"]
        replaced = SUPERSEDED.fullmatch(status)
        if status not in ("active", "gap") and not replaced:
            fail("status must be active, gap or superseded: <id>")
        if replaced and (replaced[1] == row["id"] or replaced[1] not in ids):
            fail(f"superseded by {replaced[1]}, which is not another row")
        if status == "active" and not check:
            fail("an active row needs a check")
        if status == "gap" and not MISSING.search(row["decision"]):
            fail("a gap row's decision must say `Missing: <what>`")
        if check and (problem := check_problem(root, check)):
            fail(problem)
    return rows, errors


class DecisionsLedger(unittest.TestCase):
    def test_the_ledger_is_valid(self):
        rows, errors = validate()
        self.assertEqual(errors, [])
        self.assertTrue(rows)

    def test_every_axiom_has_a_row(self):
        text = (ROOT / AXIOMS).read_text(encoding="utf-8")
        axioms = {m[1] for m in re.finditer(r"^\|\s*([A-Z]\d+)\s*\|", text, re.M)}
        self.assertEqual(axioms, {f"A{i}" for i in range(9)})
        rows, _ = validate()
        covered = {row["axiom"] for row in rows if row["id"] == f"AX-{row['axiom']}"}
        self.assertEqual(axioms - covered, set())


class LedgerRules(unittest.TestCase):
    """The validator itself, on a ledger in a temporary folder."""

    HEADER = ",".join(COLUMNS)
    GOOD = "AX-A1,Something true.,A1,owner,2026-11-20,tests/test_x.py::Things::test_thing,active"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "tests").mkdir()
        (self.root / AXIOMS).write_text("# Axioms\n\n| | Axiom |\n|---|---|\n| A1 | One. |\n", encoding="utf-8")
        self.write_test("class Things:\n    def test_thing(self):\n        pass\n")

    def write_test(self, text):
        (self.root / "tests" / "test_x.py").write_text(text, encoding="utf-8")

    def errors(self, *lines, header=HEADER):
        (self.root / LEDGER).write_text("\n".join((header, *lines)) + "\n", encoding="utf-8")
        return validate(self.root)[1]

    def test_a_good_row_passes(self):
        self.assertEqual(self.errors(self.GOOD), [])

    def test_deleting_the_test_a_row_names_fails(self):
        self.write_test("class Things:\n    def test_other(self):\n        pass\n")
        self.assertEqual(self.errors(self.GOOD), ["[AX-A1] test does not exist: tests/test_x.py::Things::test_thing"])

    def test_naming_a_test_that_does_not_exist_fails(self):
        for check, reason in (("tests/test_x.py::Things::test_nothing", "test does not exist"),
                              ("tests/test_x.py::Nobody::test_thing", "test class does not exist"),
                              ("tests/test_y.py::Things::test_thing", "check file does not exist"),
                              ("tests/test_x.py::test_thing", "check must be")):
            errors = self.errors(self.GOOD.replace("tests/test_x.py::Things::test_thing", check))
            self.assertEqual(len(errors), 1, check)
            self.assertIn(reason, errors[0])

    def test_each_rule_names_the_row_it_breaks(self):
        cases = {
            "header must be": dict(lines=[self.GOOD], header="id,decision,source,date,check,status"),
            "duplicate id": dict(lines=[self.GOOD, self.GOOD]),
            "is not a row or heading": dict(lines=[self.GOOD.replace(",A1,", ",A9,")]),
            "date must be": dict(lines=[self.GOOD.replace("2026-11-20", "20 Nov")]),
            "an active row needs a check": dict(lines=["AX-A1,Something true.,A1,owner,2026-11-20,,active"]),
            "Missing: <what>": dict(lines=["AX-A1,Not true yet.,A1,owner,2026-11-20,,gap"]),
            "status must be": dict(lines=[self.GOOD.replace(",active", ",maybe")]),
            "which is not another row": dict(lines=[self.GOOD.replace(",active", ",superseded: AX-A1")]),
            "only `check` may be empty": dict(lines=["AX-A1,,A1,owner,2026-11-20,,gap"]),
            "columns": dict(lines=["AX-A1,Something true.,A1"]),
        }
        for reason, case in cases.items():
            errors = self.errors(*case["lines"], header=case.get("header", self.HEADER))
            self.assertTrue(any(reason in e for e in errors), (reason, errors))

    def test_a_gap_row_that_says_what_is_missing_passes_without_a_check(self):
        self.assertEqual(self.errors("AX-A1,Not true yet. Missing: a test.,A1,owner,2026-11-20,,gap"), [])


# A2: names the room must not hold. Projects the room has served and their project-specific role names; add a project's
# here when it joins. The login, host path, worktree path and the folder projects live in come from this repo's room.toml.
OTHER_PROJECTS = ("gamemirage", "magazine", "art-tech")
# Today's violations, each counted. Fixing one means taking it off this list; a new one fails the test (commonroom#218).
EXCEPTIONS = {
    ("commonroom/workers.py", "art-tech"): 1,   # one project's role keeps the owner's MCP connectors
    ("commonroom/reddit.py", "kgaydhani"): 1,   # the owner's login in the research user agent
}
ISSUE_REF = re.compile(r"[\w./-]+#\d+")


def project_tokens(root: Path = ROOT) -> set[str]:
    cfg = tomllib.loads((root / "room.toml").read_text(encoding="utf-8"))
    tokens = {t.lower() for t in OTHER_PROJECTS}
    tokens.add(cfg["project"]["repo"].split("/")[0].lower())
    worktrees = cfg["project"].get("worktrees")
    if worktrees:
        tokens.add(worktrees.replace("\\", "/").rstrip("/").lower())
    host = (cfg.get("share") or {}).get("host")
    if host:   # the host project's path, its name, and the folder projects live in
        host = host.replace("\\", "/").rstrip("/").lower()
        tokens |= {host, *host.rsplit("/", 1)}
    tokens.discard(cfg["project"]["name"].lower())   # the room may name itself
    return {t for t in tokens if t}


def docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def code_texts(source: str) -> list[str]:
    """The identifiers and string literals of a module, without comments, docstrings or issue references."""
    tree = ast.parse(source)
    skip = docstrings(tree)
    texts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            texts.append(ISSUE_REF.sub("", node.value.replace("\\", "/")))
        elif isinstance(node, ast.Name):
            texts.append(node.id)
        elif isinstance(node, ast.Attribute):
            texts.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(node.name)
        elif isinstance(node, ast.arg):
            texts.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            texts.append(node.arg)
        elif isinstance(node, ast.alias):
            texts += [node.name, node.asname or ""]
    return texts


def count_tokens(texts: list[str], tokens: set[str]) -> collections.Counter:
    found = collections.Counter()
    for text in texts:
        text = text.lower()
        for token in tokens:
            found[token] += text.count(token) + (text.count(token.replace("-", "_")) if "-" in token else 0)
    return +found


def room_violations(root: Path = ROOT) -> dict[tuple[str, str], int]:
    tokens = project_tokens(root)
    found = {}
    for path in sorted((root / "commonroom").rglob("*")):
        if path.suffix == ".py":
            texts = code_texts(path.read_text(encoding="utf-8"))
        elif path.suffix in (".html", ".js", ".css", ".json", ".toml"):
            texts = [ISSUE_REF.sub("", path.read_text(encoding="utf-8").replace("\\", "/"))]
        else:
            continue
        for token, n in count_tokens(texts, tokens).items():
            found[(path.relative_to(root).as_posix(), token)] = n
    return found


class RoomKnowsNoProject(unittest.TestCase):
    """A2: no room module holds a project's name, path or login in code or a string. Comments, docstrings and issue
    references such as `project#121` are allowed."""

    def test_room_code_names_no_project_beyond_the_counted_exceptions(self):
        self.assertEqual(room_violations(), EXCEPTIONS)

    def test_the_scan_sees_code_and_strings_but_not_comments_or_issue_references(self):
        source = ('"""Docstring: gamemirage."""\n'
                  '# a comment about gamemirage\n'
                  'A = "gamemirage#121 fixed"\n'
                  'B = r"D:\\Dev\\shop"\n'
                  'def art_tech(gamemirage_root):\n'
                  '    return f"{B}/magazine"\n')
        found = count_tokens(code_texts(source), {"gamemirage", "magazine", "art-tech", "d:/dev"})
        self.assertEqual(found, {"gamemirage": 1, "magazine": 1, "art-tech": 1, "d:/dev": 1})

    def test_the_tokens_come_from_room_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "room.toml").write_text('[project]\nname = "room"\nrepo = "someone/room"\n'
                                                 'worktrees = "E:/work/room-wt"\n[share]\nhost = "E:\\\\work\\\\shop"\n',
                                                 encoding="utf-8")
            tokens = project_tokens(Path(tmp))
        self.assertTrue({"someone", "e:/work/shop", "e:/work", "shop", "e:/work/room-wt"} <= tokens)
        self.assertNotIn("room", tokens)


class CodeDecidesWhatRuns(unittest.TestCase):
    """A6: deciding what runs is plain code. A full pass picks a job with every way to a model shut."""

    def test_a_full_pass_picks_a_job_without_calling_a_model(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(Path(tmp.name) / "room.sqlite3", synchronous="OFF")
        self.addCleanup(ledger.close)
        now = time.time()
        ledger.record_usage([usage.observation(p, w, 1, now + left, "test") for p in ("claude", "codex")
                             for w, left in (("five_hour", 4 * 3600), ("seven_day", 6 * 86400))], now=now)
        board = FakeBoard([issue(4, ["status:ready", "role:builder"])])
        d = dispatch.Dispatcher(cfg_with(tmp.name), ledger, board=board, log=lambda *_: None)
        executed = []
        d.run_job = lambda job, provider, model: executed.append((job, provider)) or {}
        model = mock.Mock(side_effect=AssertionError("a model was called while deciding what runs"))
        process = mock.Mock(side_effect=AssertionError("a process was started while deciding what runs"))
        with mock.patch.object(dispatch, "run_worker", model), mock.patch.object(workers, "run_worker", model), \
                mock.patch.object(usage, "claude_screen_limits", model), mock.patch.object(usage, "codex_limits", model), \
                mock.patch.object(subprocess, "Popen", process), mock.patch.object(subprocess, "run", process):
            report = d.run_once()
        model.assert_not_called()
        process.assert_not_called()
        self.assertEqual(report["job"]["kind"], "build")
        self.assertEqual([(job.issue["number"], provider) for job, provider in executed], [(4, "codex")])


if __name__ == "__main__":
    unittest.main()
