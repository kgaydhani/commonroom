"""commonroom#216: every prompt the dispatcher can send a worker, rendered and compared byte for byte with the fixtures
in tests/golden_prompts/. The fixtures were first rendered by the f-strings in dispatch.py, before the prompt text moved
to commonroom/ways/prompts/, so a match shows the move changed nothing a worker is told.

A deliberate change to what workers are told re-renders the fixtures with `ROOM_REGEN_PROMPTS=1` and shows the diff in
its PR. Paths that come from the machine (the room's checkout, the run's worktree) are pinned here, not loosened in the
comparison."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import config, dispatch
from commonroom.ledger import Ledger

FIXTURES = Path(__file__).resolve().parent / "golden_prompts"
REGEN = os.environ.get("ROOM_REGEN_PROMPTS") == "1"

# The project settings that change a prompt: trusted logins, mission and milestone, `[checks]` and `[prompts]`.
CONFIGS = {
    # No repo owner and no `[project] trusted`: nobody's GitHub text is trusted. No milestone, mission or checks.
    "bare": {"project": {"name": "p", "repo": ""}, "goal": {"milestone": None}},
    # The usual project: the owner is the one trusted login, CI on, no test command.
    "plain": {"project": {"name": "p", "repo": "o/p", "mission": "Ship the thing."}, "goal": {"milestone": "M"}},
    # A test command with CI.
    "ci-test": {"project": {"name": "p", "repo": "o/p"}, "goal": {"milestone": "M"}, "checks": {"test": "make test"}},
    # Everything on: a co-editor, no CI (the reviewer's test run merges), and both `[prompts]` keys.
    "full": {"project": {"name": "p", "repo": "o/p", "mission": "Ship the thing", "trusted": ["co-editor"]},
             "goal": {"milestone": "M"}, "checks": {"test": "python -m unittest", "ci": False},
             "prompts": {"headless": "Headless guidance for this project.", "codex": "Codex sandbox guidance."}},
}
OPEN_PR = {"number": 5, "state": "OPEN"}
REVIEW_PR = {"number": 5, "headRefName": "i7-thing", "headRefOid": "abc1234"}


def issue(n, comments=()):
    return {"number": n, "state": "OPEN", "title": "Make the thing", "labels": [], "comments": list(comments)}


class Board:
    def pr_for_branch(self, branch): return None


# name -> (job, branch, open PR on the branch)
JOBS = {
    "build": (lambda: dispatch.Job("build", "builder", issue=issue(7)), "i7-make-the-thing", None),
    "build-pr": (lambda: dispatch.Job("build", "builder", issue=issue(7)), "i7-make-the-thing", OPEN_PR),
    "merge-main": (lambda: dispatch.Job("build", "builder", issue=issue(
        7, [{"body": f"{dispatch.MARK} PR #5 conflicts with main: {dispatch.MERGE_MAIN}."}])), "i7-make-the-thing", OPEN_PR),
    "scout": (lambda: dispatch.Job("build", "scout", issue=issue(8)), "i8-sweep", None),
    "role": (lambda: dispatch.Job("role", "playtester", issue=issue(9)), None, None),
    "review": (lambda: dispatch.Job("review", "reviewer", pr=dict(REVIEW_PR)), "i7-thing", None),
    "audit": (lambda: dispatch.Job("audit", "auditor", reason="periodic audit"), None, None),
    "director": (lambda: dispatch.Job("direct", "director", reason="#3 escalated to the director"), None, None),
}
# name -> (job, branch, earlier run). Only Claude sessions resume.
CONTINUATIONS = {
    "build": (JOBS["build"][0], "i7-make-the-thing", {"status": "stopped", "note": "stopped by the room: owner active"}),
    "review": (JOBS["review"][0], "i7-thing", {"status": "limited", "note": None}),
    "role": (JOBS["role"][0], None, {"status": "stopped", "note": "stopped by the room: pause"}),
    "director": (JOBS["director"][0], None, {"status": "limited", "note": ""}),
}


class GoldenPrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        # The audit prompt names room.py next to the module's own checkout: pin that checkout.
        pin = mock.patch.object(dispatch, "__file__", "X:/room/commonroom/dispatch.py")
        pin.start()
        self.addCleanup(pin.stop)

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, name, provider="claude"):
        cfg = config.merge(config.DEFAULTS, CONFIGS[name] if isinstance(name, str) else name)
        cfg["_root"] = self.tmp.name
        d = dispatch.Dispatcher(cfg, self.ledger, board=Board(), log=lambda *_: None)
        d.room_root = Path("X:/room")      # the checkout `room.py check` runs from
        d._worktree = "X:/wt/run"          # the run's working copy, where the auditor's report goes
        d._provider = provider
        return d

    def rendered(self) -> dict[str, str]:
        """Every prompt, as `run_job` sends it: the task prompt, why it matters, and the headless note."""
        out = {}
        for name in CONFIGS:
            for provider in ("claude", "codex"):
                for kind, (job, branch, pr) in JOBS.items():
                    d = self.dispatcher(name, provider)
                    d.board.pr_for_branch = lambda branch, pr=pr: pr   # past the trust filter: any config renders it
                    out[f"{name}-{provider}-{kind}"] = d.prompt(job(), branch) + d.why() + d.headless_note()
            for kind, (job, branch, earlier) in CONTINUATIONS.items():
                d = self.dispatcher(name)
                out[f"{name}-continue-{kind}"] = d.continuation_prompt(job(), branch, earlier) + d.headless_note()
        out["steer"] = dispatch.steer_reason({"director": {}})
        out["steer-log-number"] = dispatch.steer_reason({"director": {"log_issue": 376}})
        out["steer-log-ref"] = dispatch.steer_reason({"director": {"log_issue": "o/game#376"}})
        out["bulletin"] = self.bulletin()
        for env in ("", "docs/machine.md"):
            out["sweep" + ("-environment" if env else "")] = self.sweep(env)
        return out | self.edges()

    def edges(self) -> dict[str, str]:
        """Branches the reviewer of #216 found the grid above missed. Unlike the rest, these fixtures were rendered by
        the code after the move: the reviewer's differential run of old and new code showed them equal."""
        plain = CONFIGS["plain"]
        cases = {   # name -> (config, job, branch, open PR)
            "edge-review-no-ci-no-test": (plain | {"checks": {"ci": False}}, JOBS["review"][0], "i7-thing", None),
            "edge-review-odd-branch": (plain, lambda: dispatch.Job("review", "reviewer", pr={
                "number": 5, "headRefName": "fix-thing", "headRefOid": "abc1234"}), "fix-thing", None),
            "edge-build-pr-closed": (plain, JOBS["build"][0], "i7-make-the-thing", {"number": 5, "state": "CLOSED"}),
            "edge-why-mission-only": ({"project": plain["project"], "goal": {"milestone": None}}, JOBS["build"][0],
                                      "i7-make-the-thing", None),
            "edge-audit-every-0": (plain | {"audit": {"every_hours": 0}}, JOBS["audit"][0], None, None),
            "edge-audit-every-fraction": (plain | {"audit": {"every_hours": 5.5}}, JOBS["audit"][0], None, None),
        }
        out = {}
        for name, (cfg, job, branch, pr) in cases.items():
            d = self.dispatcher(cfg)
            d.board.pr_for_branch = lambda branch, pr=pr: pr
            out[name] = d.prompt(job(), branch) + d.why() + d.headless_note()
        # A review continuation whose PR carries no head sha reads the branch's remote head instead.
        d = self.dispatcher("plain")
        d.remote_head = lambda branch: "def5678"
        review = dispatch.Job("review", "reviewer", pr={"number": 5, "headRefName": "i7-thing"})
        out["edge-continue-review-no-sha"] = d.continuation_prompt(review, "i7-thing", {"status": "limited"}) + d.headless_note()
        return out

    def bulletin(self) -> str:
        d = self.dispatcher("plain")
        d.cfg["bulletin"] = {"issue": 286, "deadlines": ["10:00"]}
        d.board.issue_comments = lambda n, since: []
        now = 1767351600.0   # pinned with the deadline window, so the local timezone doesn't matter
        with mock.patch.object(dispatch, "bulletin_window", lambda deadlines, at: (0.0, now - 3600, "10:00")):
            return d.bulletin_missed(0.0, now=now)

    def sweep(self, environment: str) -> str:
        d = self.dispatcher("plain")
        d.cfg["scout"] = {"environment": environment} if environment else {}
        calls = []
        with mock.patch.object(dispatch.board_mod, "gh", lambda args, *a, **k: calls.append(args) or "https://x/issues/4"), \
                mock.patch.object(dispatch.time, "strftime", lambda fmt, *a: "2026-01-02"):
            d.create_sweep_issue("MCP servers & tools")
        return calls[0][calls[0].index("--body") + 1]

    def test_every_prompt_matches_its_fixture_byte_for_byte(self):
        rendered = self.rendered()
        if REGEN:
            FIXTURES.mkdir(exist_ok=True)
            for old in FIXTURES.glob("*.txt"):
                old.unlink()
            for name, text in rendered.items():
                (FIXTURES / f"{name}.txt").write_bytes(text.encode("utf-8"))
        self.assertEqual(sorted(p.stem for p in FIXTURES.glob("*.txt")), sorted(rendered))
        for name, text in rendered.items():
            with self.subTest(prompt=name):
                self.assertEqual(text, (FIXTURES / f"{name}.txt").read_bytes().decode("utf-8"))

    def test_the_cases_cover_every_dimension(self):
        """Build with and without a PR, merge-main, scout, role, review, audit and director; both providers; trusted
        logins, CI, a test command and both `[prompts]` keys on and off; and the variants each one switches."""
        rendered = self.rendered()
        self.assertEqual(len(rendered), len(CONFIGS) * (2 * len(JOBS) + len(CONTINUATIONS)) + 6 + 7)
        self.assertIn("Run `the project's tests` here", rendered["edge-review-no-ci-no-test"])
        self.assertIn("(it implements issue #?)", rendered["edge-review-odd-branch"])
        self.assertNotIn("A PR already exists", rendered["edge-build-pr-closed"])
        self.assertIn("it serves the mission: Ship the thing. Judge", rendered["edge-why-mission-only"])
        self.assertIn("now at head def5678.", rendered["edge-continue-review-no-sha"])
        self.assertIn("--hours 1`", rendered["edge-audit-every-0"])
        self.assertIn("--hours 5.5`", rendered["edge-audit-every-fraction"])
        self.assertNotIn("@", rendered["bare-claude-build-pr"])
        self.assertIn("raised by @co-editor, @o,", rendered["full-claude-build-pr"])
        self.assertIn("This repo has no CI", rendered["full-claude-review"])
        self.assertNotIn("This repo has no CI", rendered["ci-test-claude-review"])
        self.assertIn("Codex sandbox guidance.", rendered["full-codex-review"])
        self.assertIn("Headless guidance for this project.", rendered["full-claude-build"])
        self.assertIn("conflicts with main. This is a merge-main run", rendered["plain-codex-merge-main"])
        self.assertIn("Why this matters", rendered["plain-claude-build"])
        self.assertNotIn("Why this matters", rendered["bare-claude-build"])
        self.assertIn("X:/room/room.py\" check", rendered["ci-test-claude-build"])
        self.assertIn("(docs/machine.md)", rendered["sweep-environment"])


if __name__ == "__main__":
    unittest.main()


class PublicRepoRule(unittest.TestCase):
    """commonroom#316: the build and review prompts say the repo may be public, and what must stay out of it."""
    def test_build_and_review_templates_carry_the_public_repo_rule(self):
        prompts = Path(__file__).resolve().parent.parent / "commonroom" / "ways" / "prompts"
        for name in ("build.md", "build-codex.md", "review.md"):
            text = (prompts / name).read_text(encoding="utf-8")
            self.assertIn("This repo may be public", text, name)
            self.assertIn("another project's", text, name)
        self.assertIn("CHANGES verdict", (prompts / "review.md").read_text(encoding="utf-8"))
        self.assertIn("scratch home with made-up projects", (prompts / "build.md").read_text(encoding="utf-8"))

    def test_rendered_prompts_carry_it(self):
        for name in ("full-claude-build", "plain-codex-build", "plain-claude-review", "bare-claude-build"):
            text = (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")
            self.assertIn("This repo may be public", text, name)
