"""commonroom#112: every method a test board fake defines must exist on the real GitHubBoard and accept the
fake's call shape. PR #110 once added a method only to a fake, and 219 tests passed. Nothing here calls GitHub."""
import inspect
import unittest

from commonroom import dispatch
from commonroom.dispatch import GitHubBoard

from test_dispatch import FakeBoard, LabelBoard
from test_prompts import Board as PromptsBoard
from test_self_serve import MergeBoard

# Every board fake the tests hand to a Dispatcher. A new fake goes here too.
FAKES = [FakeBoard, LabelBoard, MergeBoard, PromptsBoard]

# Fake-only helper methods that aren't board methods: name -> why. Empty today.
ALLOWED = {}

POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


def shape(fn):
    """(number of positional parameters after self, names of parameters a caller may pass by keyword)."""
    params = list(inspect.signature(fn).parameters.values())[1:]
    positional = [p for p in params if p.kind in POSITIONAL]
    keywords = {p.name for p in params if p.default is not p.empty or p.kind == p.KEYWORD_ONLY}
    return len(positional), keywords


def mismatches(fake, real=GitHubBoard):
    """What in `fake` disagrees with `real`, one line per method."""
    problems = []
    for name, fn in inspect.getmembers(fake, inspect.isfunction):
        if name.startswith("_") or name in ALLOWED:
            continue
        where = f"{fake.__module__}.{fake.__name__}.{name}"
        actual = getattr(real, name, None)
        if not callable(actual):
            problems.append(f"{where}: {real.__name__} has no method {name}")
            continue
        (fake_n, fake_kw), (real_n, real_kw) = shape(fn), shape(actual)
        if fake_n != real_n:
            problems.append(f"{where}: fake takes {fake_n} positional parameters, {real.__name__}.{name} takes {real_n}")
        missing = sorted(fake_kw - set(inspect.signature(actual).parameters))
        if missing:
            problems.append(f"{where}: {real.__name__}.{name} has no keyword parameter {', '.join(missing)}")
    return problems


class BoardContract(unittest.TestCase):
    def test_every_fake_matches_the_real_board(self):
        for fake in FAKES:
            with self.subTest(fake=fake.__name__):
                self.assertEqual(mismatches(fake), [])

    def test_a_drifted_fake_is_named_with_its_method_and_difference(self):
        class Drifted:
            behind = {}                                    # attributes are test state, not methods

            def set_priority(self, n, priority): pass

            def merge(self, n, admin): pass

            def create_issue(self, title, body, labels=(), assignee=None): pass

        found = "\n".join(mismatches(Drifted))
        self.assertIn("Drifted.set_priority: GitHubBoard has no method set_priority", found)
        self.assertIn("Drifted.merge: fake takes 2 positional parameters, GitHubBoard.merge takes 1", found)
        self.assertIn("Drifted.create_issue: GitHubBoard.create_issue has no keyword parameter assignee", found)
        self.assertNotIn("behind", found)


if __name__ == "__main__":
    unittest.main()


class SetMilestoneArgv(unittest.TestCase):
    def test_builds_gh_issue_edit_argv(self):
        calls = []
        orig = dispatch.board_mod.gh
        dispatch.board_mod.gh = lambda args, *a, **k: calls.append(list(args))
        try:
            GitHubBoard("o/t").set_milestone(7, "Room v3")
        finally:
            dispatch.board_mod.gh = orig
        self.assertEqual(calls, [["issue", "edit", "7", "--repo", "o/t", "--milestone", "Room v3"]])
