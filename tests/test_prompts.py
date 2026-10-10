"""commonroom#45: project-specific prompt guidance comes from room.toml `[prompts]` and `[checks] test`, so
commonroom's workers aren't told about Godot or gamemirage's test scripts, and gamemirage's keep their wording."""
import tempfile
import tomllib
import unittest
from pathlib import Path

from commonroom import config, dispatch
from commonroom.ledger import Ledger

REPO = Path(__file__).resolve().parent.parent
GODOT = "D:/Dev/tools/godot/Godot_v4.7.2-stable_win64_console.exe"
# The lines gamemirage's room.toml carries to keep today's wording (the PR for #45 lists them).
GAMEMIRAGE_PROMPTS = {
    "headless": "The slow suite (`tools/test_slow.sh`) runs in CI; run it locally only if your task requires it, in the foreground.",
    "codex": ("Git Bash can't start in your sandbox (an upstream Codex limitation), so run the tests with "
              "`python tools/test.py` if it exists, otherwise call Godot directly: "
              "`\"$GODOT\" --headless --path game -s res://tests/run_tests.gd` (GODOT is "
              f"{GODOT}). No browser (Chrome, Edge, Playwright) can start in your "
              "sandbox; the web export and serve.py do work. Rendering does work: for visual checks use "
              "`--write-movie` off-screen (`--position -10000,-10000`), create the output folder first (Godot won't, and "
              "silently writes nothing), use absolute paths inside your checkout, and ignore `user://` log errors. Look at "
              "the frames you render."),
}
GAME_ONLY = ("Godot", "tools/test.sh", "tools/test_slow.sh", "tools/test.py")


class Board:
    def __init__(self):
        self.comments, self.labels = [], []

    def pr_for_branch(self, branch): return None
    def set_labels(self, n, add=(), remove=()): self.labels.append((n, tuple(add), tuple(remove)))
    def comment(self, n, body): self.comments.append((n, body))


def issue(n, labels=()):
    return {"number": n, "state": "OPEN", "title": "t", "labels": [{"name": l} for l in labels], "comments": []}


RED = {"statusCheckRollup": [{"name": "tests", "conclusion": "FAILURE", "detailsUrl": "https://x/actions/runs/1"}]}


class PromptGuidance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, overrides):
        cfg = config.merge(config.DEFAULTS, overrides)
        cfg["_root"] = self.tmp.name
        return dispatch.Dispatcher(cfg, self.ledger, board=Board(), log=lambda *_: None)

    def commonroom(self):
        with open(REPO / "room.toml", "rb") as f:
            return self.dispatcher(tomllib.load(f))

    def gamemirage(self):
        return self.dispatcher({"project": {"name": "gamemirage", "repo": "o/gamemirage"}, "goal": {"milestone": "M"},
                                "checks": {"test": "bash tools/test.sh"}, "prompts": GAMEMIRAGE_PROMPTS})

    def prompts(self, d):
        """Every prompt the dispatcher can give a worker, as it would be sent (with the headless note)."""
        jobs = [dispatch.Job("build", "builder", issue=issue(7)), dispatch.Job("build", "scout", issue=issue(7)),
                dispatch.Job("role", "playtester", issue=issue(7)),
                dispatch.Job("review", "reviewer", pr={"number": 5, "headRefName": "i7-thing"}),
                dispatch.Job("audit", "auditor", reason="periodic audit"), dispatch.Job("direct", "director", reason="x")]
        out = []
        for provider in ("claude", "codex"):
            d._provider = provider
            out += [d.prompt(job, "i7-thing") + d.headless_note() for job in jobs]
        return out

    def bounce(self, d):
        d.handle_red_ci({"number": 5}, RED, 7, [issue(7, ["status:review"])])
        return d.board.comments[-1][1]

    def test_gamemirage_keeps_its_guidance(self):
        d = self.gamemirage()
        self.assertIn("The slow suite (`tools/test_slow.sh`) runs in CI", d.headless_note())
        self.assertIn("Stop only processes you started, by PID.", d.headless_note())
        d._provider = "codex"
        build = d.prompt(dispatch.Job("build", "builder", issue=issue(7)), "i7-thing")
        self.assertIn(GAMEMIRAGE_PROMPTS["codex"] + " If something in the sandbox is impossible", build)
        self.assertIn(GODOT, build)
        self.assertIn("Git Bash can't start", d.prompt(dispatch.Job("review", "reviewer", pr={"number": 5, "headRefName": "i7-x"}), None))
        self.assertIn("run `bash tools/test.sh` before pushing", self.bounce(d))

    def test_codex_build_with_open_pr_is_never_told_to_merge(self):
        """commonroom#51: "merge origin/main" contradicted "do not commit", and Codex copied main's files in instead."""
        d = self.gamemirage()
        d.board.pr_for_branch = lambda branch: {"number": 5, "state": "OPEN"}
        d._provider = "codex"
        build = d.prompt(dispatch.Job("build", "builder", issue=issue(7)), "i7-thing")
        self.assertIn("The room handles any conflicts with main", build)
        for word in ("merge origin/main", "git fetch", "git merge", "push to the same branch"):
            self.assertNotIn(word, build)
        self.assertIn(GAMEMIRAGE_PROMPTS["codex"], build)                 # the game's Codex guidance stays
        d._provider = "claude"                                            # Claude's wording is unchanged
        build = d.prompt(dispatch.Job("build", "builder", issue=issue(7)), "i7-thing")
        self.assertIn("If the PR conflicts with main, first merge origin/main into the branch and resolve the conflicts, "
                      "then rerun the tests.", build)

    def test_claude_reviews_get_no_codex_note(self):
        d = self.gamemirage()
        d._provider = "claude"
        self.assertNotIn("Git Bash", d.prompt(dispatch.Job("review", "reviewer", pr={"number": 5, "headRefName": "i7-x"}), None))

    def test_commonroom_prompts_name_no_game_tooling(self):
        d = self.commonroom()
        texts = self.prompts(d) + [self.bounce(d)]
        for text in texts:
            for word in GAME_ONLY:
                self.assertNotIn(word, text)
        self.assertIn("run `python -m unittest discover -s tests` before pushing", texts[-1])

    def test_unset_keys_add_nothing(self):
        d = self.dispatcher({"project": {"name": "p", "repo": "o/p"}, "goal": {"milestone": "M"}})
        self.assertEqual(dispatch.HEADLESS_NOTE, d.headless_note())
        self.assertEqual("", d.codex_note())


if __name__ == "__main__":
    unittest.main()
