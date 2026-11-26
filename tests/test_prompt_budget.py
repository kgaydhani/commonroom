"""Axiom A9 (commonroom#231): a token budget on the text the room itself adds to a run.

The fixtures in tests/golden_prompts/ are every prompt the dispatcher renders (tests/test_prompt_golden.py holds them
equal to the templates' output), so each one is held to `prompt_max_tokens`. The templates in commonroom/ways/prompts/
together are held to `templates_max_tokens`. Both limits are in commonroom/ways/policy.toml. A token is four characters,
rounded up."""
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from commonroom import policy

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "golden_prompts"
TEMPLATES = ROOT / "commonroom" / "ways" / "prompts"
COMMENTS = ROOT / "commonroom" / "ways" / "comments"   # the text the room posts as issue comments (commonroom#223)
HANDSHAKE = ROOT / "commonroom" / "ways" / "handshake"   # the room folder's files (commonroom#253) and the session hook's (#262)


def tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")   # universal newlines: a CRLF checkout counts the same as LF


def over_budget(fixtures: Path, templates: Path, prompt_limit: int, templates_limit: int, also: tuple = ()) -> list[str]:
    """What is over budget: each rendered prompt in `fixtures` over `prompt_limit` tokens, naming the file and its
    size, and every file in `templates` and the `also` folders together if over `templates_limit`. Empty when all is
    inside."""
    problems = [f"{p.name} is {n} tokens, over the {prompt_limit}-token budget for one prompt"
                for p in sorted(fixtures.glob("*.txt")) if (n := tokens(read(p))) > prompt_limit]
    folders = (templates, *also)
    total = tokens("".join(read(p) for folder in folders for p in sorted(folder.iterdir()) if p.is_file()))
    if total > templates_limit:
        problems.append(f"{' + '.join(f.name + '/' for f in folders)} is {total} tokens, over the {templates_limit}-token "
                        f"budget for all templates")
    return problems


class Budget(unittest.TestCase):
    def test_the_rooms_prompt_text_stays_inside_its_token_budget(self):
        self.assertGreater(len(list(FIXTURES.glob("*.txt"))), 0, f"no rendered prompts in {FIXTURES}")
        problems = over_budget(FIXTURES, TEMPLATES, policy.DEFAULTS["prompt_max_tokens"],
                               policy.DEFAULTS["templates_max_tokens"], also=(HANDSHAKE, COMMENTS))
        self.assertEqual(problems, [], "; ".join(problems))

    def test_the_handshake_templates_count_toward_the_same_budget(self):
        self.assertEqual(sorted(p.name for p in HANDSHAKE.iterdir()), ["README.md", "answer-back.md", "charter.md", "cold-return.md", "compacted.md", "decisions-more.md", "decisions.md", "digest.md", "handoff-offer.md", "handoff.md", "hands.md", "place-start.md", "places-recent.md", "reply.md",
                                                                     "session-start.md"])
        with tempfile.TemporaryDirectory() as tmp:
            handshake = Path(tmp) / "handshake"
            shutil.copytree(HANDSHAKE, handshake)
            used = tokens("".join(read(p) for f in (TEMPLATES, handshake) for p in sorted(f.iterdir()) if p.is_file()))
            limit = policy.DEFAULTS["templates_max_tokens"]
            with open(handshake / "session-start.md", "a", encoding="utf-8") as f:   # the session hook's text counts too
                f.write("x" * 4 * (limit + 1 - used))
            self.assertEqual(over_budget(FIXTURES, TEMPLATES, 700, limit, also=(handshake,)),
                             [f"prompts/ + handshake/ is {limit + 1} tokens, over the {limit}-token budget for all templates"])

    def test_the_comment_templates_count_toward_the_same_budget(self):
        self.assertGreater(len(list(COMMENTS.glob("*.md"))), 0)
        with tempfile.TemporaryDirectory() as tmp:
            comments = Path(tmp) / "comments"
            shutil.copytree(COMMENTS, comments)
            used = tokens("".join(read(p) for f in (TEMPLATES, HANDSHAKE, comments) for p in sorted(f.iterdir()) if p.is_file()))
            limit = policy.DEFAULTS["templates_max_tokens"]
            with open(comments / "rescue.md", "a", encoding="utf-8") as f:
                f.write("x" * 4 * (limit + 1 - used))
            self.assertEqual(over_budget(FIXTURES, TEMPLATES, 700, limit, also=(HANDSHAKE, comments)),
                             [f"prompts/ + handshake/ + comments/ is {limit + 1} tokens, over the {limit}-token budget "
                              f"for all templates"])

    def test_padding_past_the_budget_fails_naming_the_file_and_its_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures, templates = Path(tmp) / "golden", Path(tmp) / "prompts"
            shutil.copytree(FIXTURES, fixtures)
            shutil.copytree(TEMPLATES, templates)
            self.assertEqual(over_budget(fixtures, templates, 700, 2500), [])
            (fixtures / "plain-claude-build.txt").write_text("x" * 2801, encoding="utf-8")
            used = tokens("".join(read(p) for p in templates.iterdir()))
            with open(templates / "build.md", "a", encoding="utf-8") as f:
                f.write("x" * 4 * (2501 - used))
            self.assertEqual(over_budget(fixtures, templates, 700, 2500),
                             ["plain-claude-build.txt is 701 tokens, over the 700-token budget for one prompt",
                              "prompts/ is 2501 tokens, over the 2500-token budget for all templates"])

    def test_tokens_are_characters_over_four_rounded_up(self):
        self.assertEqual([tokens("x" * n) for n in (0, 1, 4, 5, 2800, 2801)], [0, 1, 1, 2, 700, 701])

    def test_the_budgets_are_room_wide_only(self):
        self.assertEqual((policy.DEFAULTS["prompt_max_tokens"], policy.DEFAULTS["templates_max_tokens"]), (700, 3300))
        for name in ("prompt_max_tokens", "templates_max_tokens"):
            with self.subTest(name=name):
                self.assertIn(name, policy.GLOBAL_ONLY)
                with self.assertRaisesRegex(ValueError, f"{name} is room-wide only because it limits the room's own"):
                    policy.overrides({"policy": {name: 5000}})
        with self.assertRaisesRegex(ValueError, "unknown name 'prompt_max_token'"):
            policy.overrides({"policy": {"prompt_max_token": 5000}})


if __name__ == "__main__":
    unittest.main()
