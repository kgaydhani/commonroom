"""commonroom#226: an approval carries across a clean merge of main once the room has run the tests on the new head.
Real git and a real (fake) test command; a fake board."""
import copy
import subprocess
import unittest
from pathlib import Path

from commonroom import carry, dispatch, metrics
from commonroom.lock import claim_lock, release_lock, suite_lock
from test_dispatch import owned
from test_self_serve import Base, MergeBoard, project_cfg

PASS = "python -c \"import os, sys; print('Ran 3 tests in 0.1s'); print(); print('OK'); sys.exit(0 if os.path.exists('docs.md') and os.path.exists('f.txt') else 1)\""
FAIL = "python -c \"import sys; print('FAILED (errors=1)'); sys.exit(1)\""
HANG = "python -c \"import time; time.sleep(60)\""
BRANCH = "i222-docs"


class Board(MergeBoard):
    def __init__(self, *a):
        super().__init__(*a)
        self.posted, self.stored = [], []

    def comment(self, n, body):
        body = f"{dispatch.MARK} {body}"   # as GitHubBoard.comment does
        self.posted.append((n, body))
        self.stored.append({"createdAt": "2099-01-01T00:00:00Z", "body": body})

    def pr_detail(self, n):
        """What a fresh read of the PR returns: the comments the board recorded, not the dispatcher's in-memory copy."""
        d = copy.deepcopy(self.details[n])
        d["comments"] = d["comments"] + copy.deepcopy(self.stored)
        return owned(d, pr=True)


class Carry(Base):
    """PR #222 (20 Nov 2026): approved at one head, then main moved by a docs-only merge and the room merged main into
    the branch cleanly. A reviewer run was spent re-testing it; now code runs the tests and the approval carries."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("core.autocrlf", "false")):
            self.git("config", key, value)
        self.git("remote", "add", "origin", str(self.root))      # fetching origin reads this repo's own branches
        self.write("a.txt", "one\ntwo\n")
        self.commit("base")
        self.git("checkout", "-q", "-b", BRANCH)
        self.write("f.txt", "feature\n")
        self.tested = self.commit("feature work")                # the head the reviewer approved and tested
        self.git("checkout", "-q", "main")
        # The carry tests are about PRs: main's own suite run (commonroom#303) is off here (Base) and on in MainSuite.

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True).stdout.strip()

    def write(self, name, text):
        (self.root / name).write_bytes(text.encode())

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge_main(self, main_file="docs.md", main_text="docs\n", conflict=False):
        self.write(main_file, main_text)
        self.commit("main moves")
        self.git("checkout", "-q", BRANCH)
        self.git("merge", "-q", "--no-edit", "main")
        if conflict:
            self.write(main_file, "resolved\n")
            self.commit("Merge main into the branch")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        return head

    def cfg(self, command=PASS, on=True, ci=False, **checks):
        cfg = project_cfg(self.root, ci=ci)
        cfg["checks"].update({"test": command, **checks})
        cfg["project"]["worktrees"] = str(Path(self.tmp.name) / "wt")
        cfg["merge"] = {"carry_tested_approval": True} if on else {}
        return cfg

    def approval(self, command=PASS):
        return {"createdAt": "2026-11-20T13:00:00Z", "url": "https://github.com/o/commonroom/pull/222#c1",
                "body": f"[agent:reviewer] REVIEW: APPROVE\n\nTESTS: PASS {self.tested[:7]} {command}: Ran 524 tests "
                        f"in 80.163s OK\n\nTested on the PR head {self.tested[:7]}."}

    def board(self, head, extra=(), command=PASS, files=()):
        commits = [{"oid": self.tested, "committedDate": "2026-11-20T12:00:00Z"},
                   {"oid": head, "committedDate": "2026-11-20T14:00:00Z"}, *extra]
        detail = {"number": 222, "headRefName": BRANCH, "headRefOid": commits[-1]["oid"], "commits": commits,
                  "comments": [self.approval(command)], "statusCheckRollup": [], "files": [{"path": f} for f in files]}
        return Board([{"number": 222, "headRefName": BRANCH, "labels": []}], {222: detail})

    def carry_runs(self):
        return [r for r in self.ledger.recent_runs("commonroom", limit=50) if r["role"] == "carry"]

    def test_a_clean_merge_of_main_with_passing_tests_merges_with_no_reviewer_run(self):
        head = self.merge_main()
        board = self.board(head)
        d = self.dispatcher(self.cfg(), board)
        self.assertFalse(d.needs_review(board.details[222]))           # no reviewer is sent while the room can test it
        d.housekeeping()
        self.assertEqual(board.merged, [222])
        (n, body), = board.posted
        self.assertTrue(body.startswith(f"[room] TESTS: PASS {head} {PASS}: Ran 3 tests in 0.1s OK"), body)
        self.assertIn(f"CARRIES: APPROVE 2026-11-20T13:00:00Z TESTED {self.tested}", body)
        (run,) = self.carry_runs()
        self.assertEqual((run["provider"], run["status"], run["outputs"][0]["result"]), ("none", "done", "PASS"))
        self.assertFalse((Path(self.tmp.name) / "wt" / "carry-222").exists())   # the temporary worktree is gone
        lock = suite_lock(Path(self.ledger.path).parent, "commonroom")
        self.assertIsNone(claim_lock(lock))                             # the suite lock was let go
        release_lock(lock)

    def test_a_failing_or_unfinished_run_carries_nothing_and_the_pr_goes_back_to_review(self):
        for command, result, checks in ((FAIL, "FAIL", {}), (HANG, "INCOMPLETE", {"max_minutes": 0.05})):
            with self.subTest(result):
                head = self.merge_main(main_file=f"{result}.md")
                board = self.board(head, command=command)
                d = self.dispatcher(self.cfg(command, **checks), board)
                d.housekeeping()
                self.assertEqual(board.merged, [])
                (n, body), = board.posted
                self.assertTrue(body.startswith(f"[room] TESTS: {result} {head} {command}: "), body)
                self.assertNotIn("CARRIES:", body)
                self.assertTrue(d.needs_review(board.pr_detail(222)))    # a model reviews it, as before
                d.housekeeping()                                       # and the room doesn't run it again
                self.assertEqual(len(board.posted), 1)

    def test_a_conflicted_merge_an_extra_commit_and_ci_each_carry_nothing(self):
        conflicted = self.merge_main(main_file="f.txt", main_text="main's own f\n", conflict=True)
        self.assertTrue(dispatch.merges_main_only(self.root, conflicted, main="main"))       # the approval stands...
        self.assertFalse(dispatch.merges_main_only(self.root, conflicted, main="main", clean=True))   # ...not clean
        cases = {"conflict": (self.board(conflicted), self.cfg())}
        self.git("checkout", "-q", BRANCH)
        self.write("g.txt", "more\n")
        extra = self.commit("one more thing")
        self.git("checkout", "-q", "main")
        cases["extra commit"] = (self.board(conflicted, extra=[{"oid": extra, "committedDate": "2026-11-20T15:00:00Z"}]),
                                 self.cfg())
        cases["ci = true"] = (self.board(self.merge_main()), self.cfg(ci=True))
        for name, (board, cfg) in cases.items():
            with self.subTest(name):
                d = self.dispatcher(cfg, board)
                d.housekeeping()
                self.assertEqual((board.merged, board.posted, self.carry_runs()), ([], [], []))
                self.assertFalse(d.carry_pending(board.details[222]))

    def test_a_commit_listed_before_the_tested_one_but_added_after_it_carries_nothing(self):
        # GitHub lists a PR's commits by date. A cherry-pick keeps its old date, so it can sit before the tested commit
        # in that list though it was added after it. The walk follows first parents from the head, not the list.
        self.git("checkout", "-q", BRANCH)
        self.write("g.txt", "unreviewed\n")
        late = self.commit("cherry-picked fix")
        self.git("checkout", "-q", "main")
        head = self.merge_main()
        board = self.board(head)
        board.details[222]["commits"].insert(0, {"oid": late, "committedDate": "2026-11-20T11:00:00Z"})
        d = self.dispatcher(self.cfg(), board)
        self.assertFalse(d.carry_pending(board.details[222]))
        d.housekeeping()
        self.assertEqual((board.merged, board.posted, self.carry_runs()), ([], [], []))

    def test_a_carry_run_is_not_counted_as_a_model_run(self):
        rows = [{"role": "carry", "status": "done", "started_at": 5.0}, {"role": "reviewer", "status": "done", "started_at": 5.0}]
        self.assertEqual([r["role"] for r in metrics.model_runs(rows, 0, 10)], ["reviewer"])

    def test_every_count_of_model_runs_uses_the_one_list_of_room_roles(self):
        # The dashboard and the auditor's digest counted ("merge", "update") by hand, so a carry row read as a model run.
        root = Path(__file__).resolve().parent.parent / "commonroom"
        for path in root.glob("*.py"):
            self.assertNotIn('("merge", "update")', path.read_text(encoding="utf-8"), path.name)

    def director_board(self, stamped, files=("room.toml",)):
        head = self.merge_main()
        board = self.board(head, files=["room.toml"])
        if stamped:
            board.details[222]["comments"].append({"createdAt": "2026-11-20T13:30:00Z", "body": f"[agent:director] REVIEW: APPROVE\n\nHEAD: {self.tested[:7]}"})
        board.compare_since = lambda base, h: {"commits": [{"sha": head, "parents": [self.tested, "m" * 40]}], "files": list(files)}
        cfg = self.cfg()
        cfg["merge"]["director_paths"] = ["room.toml"]
        return board, cfg

    def test_a_director_path_pr_with_no_stamp_yet_still_carries_the_reviewer_half(self):
        board, cfg = self.director_board(stamped=False)
        d = self.dispatcher(cfg, board)
        detail = board.details[222]
        self.assertEqual(d.carry_reason(detail), "")
        self.assertTrue(d.carry_pending(detail))
        d.housekeeping()
        self.assertEqual(board.merged, [])                           # the director has not stamped it
        (n, body), = board.posted
        self.assertTrue(body.startswith("[room] TESTS: PASS"), body)
        self.assertEqual(len(self.carry_runs()), 1)
        fresh = self.dispatcher(cfg, board)
        detail = board.pr_detail(222)
        self.assertFalse(fresh.needs_review(detail))                 # no reviewer run is spent
        self.assertTrue(fresh.waits_for_director(detail))            # the merge gate is unchanged
        fresh.housekeeping()
        self.assertEqual((board.merged, len(board.posted), len(self.carry_runs())), ([], 1, 1))

    def test_a_stamp_at_the_old_head_with_no_director_path_changed_in_the_merge_merges(self):
        board, cfg = self.director_board(stamped=True, files=["docs.md"])
        d = self.dispatcher(cfg, board)
        d.housekeeping()
        self.assertEqual(board.merged, [222])

    def own_board(self, then, now):
        """Stamp at the old head, a merge of main that moved room.toml; `then`/`now` are the PR's own patches there."""
        board, cfg = self.director_board(stamped=True)
        head = board.details[222]["headRefOid"]
        def own_patches(base, sha):
            got = then if self.tested.startswith(sha) or sha == self.tested else now
            if isinstance(got, Exception):
                raise got
            return got
        board.own_patches = own_patches
        return board, cfg, head

    PATCH = "@@ -1,2 +1,3 @@ [x]\n a\n+mine\n b"
    SHIFTED = "@@ -40,2 +41,3 @@ [x]\n a\n+mine\n b"

    def test_a_stamp_carries_when_only_main_moved_the_director_path(self):
        board, cfg, head = self.own_board({"room.toml": self.PATCH}, {"room.toml": self.SHIFTED, "docs.md": None})
        d = self.dispatcher(cfg, board)
        detail = board.details[222]
        self.assertTrue(d.carried_stamp(detail))
        self.assertFalse(d.waits_for_director(detail))
        self.assertEqual(d.stamp_waits(), [])
        self.assertEqual(d.carry_reason(detail), "")           # the reviewer's approval carries too
        self.assertTrue(d.carry_pending(detail))

    def test_no_carry_when_the_pr_s_own_hunk_or_file_set_or_patch_differs(self):
        cases = {"hunk text": ({"room.toml": self.PATCH}, {"room.toml": self.PATCH + "\n+edited"}),
                 "new file": ({"docs.md": None}, {"room.toml": self.PATCH}),
                 "missing patch": ({"room.toml": self.PATCH}, {"room.toml": None}),
                 "compare error": ({"room.toml": self.PATCH}, RuntimeError("boom")),
                 "none touched": ({}, {})}
        for name, (then, now) in cases.items():
            with self.subTest(name):
                board, cfg, head = self.own_board(then, now)
                d = self.dispatcher(cfg, board)
                self.assertEqual(d.carried_stamp(board.details[222]), "")
                self.assertTrue(d.waits_for_director(board.details[222]))

    def test_a_director_path_the_pr_touches_only_at_the_new_head_is_not_carried(self):
        board, cfg, head = self.own_board({"docs.md": None}, {"docs.md": None, "room.toml": self.PATCH})
        self.assertEqual(self.dispatcher(cfg, board).carried_stamp(board.details[222]), "")

    def test_a_director_path_pr_with_a_stamp_that_cannot_be_carried_is_refused(self):
        board, cfg = self.director_board(stamped=True)               # the merge changed room.toml
        d = self.dispatcher(cfg, board)
        self.assertEqual(d.carry_reason(board.details[222]), "the director's stamp can't be carried to this head")
        self.assertTrue(d.waits_for_director(board.details[222]))

    def test_a_director_path_whose_stamp_cannot_be_carried_carries_nothing(self):
        board, cfg = self.director_board(stamped=True)
        d = self.dispatcher(cfg, board)
        d.housekeeping()
        self.assertEqual((board.merged, board.posted, self.carry_runs()), ([], [], []))
        self.assertTrue(d.needs_review(board.details[222]))

    def test_with_the_switch_unset_the_merged_tree_is_reviewed_again_as_before(self):
        board = self.board(self.merge_main())
        d = self.dispatcher(self.cfg(on=False), board)
        d.housekeeping()
        self.assertEqual((board.merged, board.posted, self.carry_runs()), ([], [], []))
        self.assertTrue(d.needs_review(board.details[222]))

    def test_a_failed_run_read_back_from_the_posted_comment_is_not_run_again(self):
        head = self.merge_main(main_file="FAIL.md")
        board = self.board(head, command=FAIL)
        d = self.dispatcher(self.cfg(FAIL), board)
        d.housekeeping()
        fresh = self.dispatcher(self.cfg(FAIL), board)               # the next pass reads the board's comments back
        detail = board.pr_detail(222)
        self.assertFalse(fresh.carry_pending(detail))
        self.assertTrue(fresh.needs_review(detail))
        fresh.housekeeping()
        self.assertEqual(len(board.posted), 1)
        self.assertEqual(len(self.carry_runs()), 1)

    def test_a_passing_run_read_back_is_mergeable_with_no_second_run(self):
        head = self.merge_main()
        board = self.board(head)
        d = self.dispatcher(self.cfg(), board)
        d.board.comment(222, carry.comment("PASS", head, PASS, "Ran 3 tests in 0.1s OK", "2026-11-20T13:00:00Z", self.tested))
        fresh = self.dispatcher(self.cfg(), board)
        self.assertTrue(fresh.tested(board.pr_detail(222)))
        self.assertFalse(fresh.carry_pending(board.pr_detail(222)))
        fresh.housekeeping()
        self.assertEqual(board.merged, [222])
        self.assertEqual((len(board.posted), len(self.carry_runs())), (1, 0))

    def test_the_comment_counts_only_from_a_trusted_author_after_the_head_it_names(self):
        head = self.merge_main()
        good = "[room] " + carry.comment("PASS", head, PASS, "Ran 3 tests OK", "2026-11-20T13:00:00Z", self.tested)
        cases = {"trusted, after the head": ({"createdAt": "2026-11-20T15:00:00Z", "body": good}, True),
                 "a stranger": ({"createdAt": "2026-11-20T15:00:00Z", "body": good, "author": {"login": "mallory"}}, False),
                 "before the last commit": ({"createdAt": "2026-11-20T13:30:00Z", "body": good}, False),
                 "another head": ({"createdAt": "2026-11-20T15:00:00Z", "body": good.replace(head, self.tested)}, False),
                 "another approval": ({"createdAt": "2026-11-20T15:00:00Z",
                                       "body": good.replace("2026-11-20T13:00:00Z", "2026-11-19T13:00:00Z")}, False)}
        for name, (comment, ok) in cases.items():
            with self.subTest(name):
                board = self.board(head)
                board.details[222]["comments"].append(comment)
                d = self.dispatcher(self.cfg(), board)
                self.assertEqual(d.tested(d.board.pr_detail(222)), ok)
                self.assertFalse(self.dispatcher(self.cfg(on=False), board).tested(board.details[222]))


class Format(unittest.TestCase):
    def test_a_real_approval_names_its_tested_sha_in_seven_characters(self):
        body = ("[agent:reviewer] REVIEW: APPROVE\n\nTESTS: PASS c922d99 python -m unittest discover -s tests: "
                "Ran 524 tests in 80.163s OK\n\nTested on the PR head c922d99 (matches headRefOid).")
        oids = ["c922d99" + "0" * 33, "abcdef0" + "1" * 33]
        self.assertEqual(carry.tested_sha(body, oids), oids[0])
        self.assertEqual(carry.tested_sha(body, oids[1:]), "")

    def test_the_comment_parses_back(self):
        body = carry.comment("PASS", "b" * 40, "python -m unittest", "Ran 1 test OK", "2026-11-20T13:00:00Z", "a" * 40)
        self.assertEqual(carry.parse("[room] " + body), {"result": "PASS", "head": "b" * 40, "line": "python -m unittest: Ran 1 test OK",
                                             "approval_at": "2026-11-20T13:00:00Z", "tested": "a" * 40})
        self.assertEqual(carry.parse("[room] [room] " + body)["result"], "PASS")   # doubled, as #252's were posted
        self.assertEqual(carry.parse("[room] " + body)["head"], "b" * 40)
        self.assertIsNone(carry.parse("[agent:builder] quoting it: " + body))
        self.assertEqual(carry.parse("[room] " + carry.comment("FAIL", "b" * 40, "x", "exit 1", "t", "a" * 40))["approval_at"], "")
        self.assertEqual(carry.summary("....\n----\nRan 524 tests in 80.163s\n\nOK\n"), "Ran 524 tests in 80.163s OK")


if __name__ == "__main__":
    unittest.main()


class RedBoard(Board):
    def __init__(self, *a):
        super().__init__(*a)
        self.created = []

    def create_issue(self, title, body, labels=(), milestone=None):
        self.created.append({"title": title, "body": body, "labels": list(labels), "milestone": milestone})
        n = 900 + len(self.created)
        self.issues.append({"number": n, "state": "OPEN", "title": title, "labels": [{"name": l} for l in labels],
                            "milestone": {"title": milestone}})
        return n

    def set_milestone(self, n, milestone): pass


class MainSuite(Carry):
    """commonroom#303: a project with a test command and no CI gets the suite run on main's head, once per sha."""

    def setUp(self):
        super().setUp()
        dispatch.Dispatcher.suite_on_main = self._suite_on_main   # Base switched it off
        self.calls, self.results = [], []
        self._run = carry.run_suite
        carry.run_suite = self.stub

    def tearDown(self):
        carry.run_suite = self._run
        super().tearDown()

    def stub(self, root, head, command, worktree, lock, max_minutes, **kw):
        if worktree.name.startswith("carry-"):
            return "PASS", "Ran 3 tests in 0.1s OK"     # the PR's own carry run, not main's
        self.calls.append(head)
        return self.results.pop(0)

    def sha(self):
        return self.git("rev-parse", "main")

    def setup_ready_pr(self):
        head = self.merge_main()
        board = RedBoard([{"number": 222, "headRefName": BRANCH, "labels": []}], {222: self.board(head).details[222]})
        return board

    def test_a_fail_on_a_new_sha_holds_merges_and_opens_one_task_and_a_pass_on_a_newer_sha_releases_it(self):
        board = self.setup_ready_pr()
        sha = self.sha()
        self.results = [("FAIL", "exit 1: FAILED (errors=1)")] * 2
        d = self.dispatcher(self.cfg(), board)
        d.housekeeping()                                   # the first FAIL: rerun, no task yet
        self.assertEqual((self.calls, board.created), ([sha], []))
        d.housekeeping()                                   # the second FAIL marks it red
        d.housekeeping()                                   # a third pass at the same sha: no rerun
        self.assertEqual((self.calls, board.merged), ([sha, sha], []))
        (task,) = board.created
        self.assertEqual(task["title"], f"Main is red: exit 1: FAILED (errors=1) failing on {sha[:7]}")
        self.assertEqual((task["labels"], task["milestone"]),
                         (["prio:top", "role:builder", "status:ready", "kind:bug"], "M"))
        self.assertIn("exit 1: FAILED (errors=1)", task["body"])
        fresh = self.dispatcher(self.cfg(), board)         # a restarted dispatcher: no rerun, no second task
        fresh.housekeeping()
        self.assertEqual((len(self.calls), len(board.created), board.merged), (2, 1, []))
        self.write("later.md", "later\n")
        newer = self.commit("main moves again")
        self.results = [("PASS", "Ran 3 tests OK")]
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual(self.calls, [sha, sha, newer])
        self.assertEqual((len(board.created), board.merged), (1, [222]))

    def test_a_fail_then_a_pass_opens_no_task_and_releases_merges(self):
        board = self.setup_ready_pr()
        self.results = [("FAIL", "exit 1: FAILED (errors=1)"), ("PASS", "Ran 3 tests OK")]
        d = self.dispatcher(self.cfg(), board)
        actions = []
        self.assertTrue(d.main_suite_red([], actions))
        self.assertIn(f"main's suite failed once on {self.sha()[:7]}; rerunning before opening a fix task", actions)
        self.assertFalse(d.main_suite_red([], []))
        self.assertEqual((len(self.calls), board.created), (2, []))
        d.housekeeping()
        self.assertEqual((len(self.calls), board.created, board.merged), (2, [], [222]))

    def test_the_red_task_lists_the_failing_tests(self):
        board = self.setup_ready_pr()
        line = carry.Line("exit 1: FAILED (failures=1)")
        line.failing = ("FAIL: test_x (tests.test_y.Y.test_x)",)
        self.results = [("FAIL", line)] * 2
        d = self.dispatcher(self.cfg(), board)
        d.main_suite_red([], [])
        d.main_suite_red([], [])
        (task,) = board.created
        self.assertIn("Failing:\n- `FAIL: test_x (tests.test_y.Y.test_x)`", task["body"])

    def stub_commands(self):
        self.commands = []
        def stub(root, head, command, worktree, lock, max_minutes, **kw):
            self.commands.append((command, max_minutes))
            return "PASS", "Ran 3 tests OK"
        carry.run_suite = stub

    def test_the_suite_runs_the_command_from_the_room_toml_at_the_sha(self):
        board = self.setup_ready_pr()
        self.stub_commands()
        self.write("room.toml", '[checks]\ntest = "py -3.14 -m unittest"\nmax_minutes = 7\n')
        self.commit("fix the test command")
        actions = []
        self.dispatcher(self.cfg(), board).main_suite_red([], actions)
        self.assertEqual(self.commands, [("py -3.14 -m unittest", 7.0)])
        self.assertFalse([a for a in actions if "room.toml" in a])

    def test_a_missing_or_broken_room_toml_at_the_sha_uses_the_live_command(self):
        board = self.setup_ready_pr()
        self.stub_commands()
        actions = []
        self.dispatcher(self.cfg(), board).main_suite_red([], actions)       # no room.toml at this sha
        self.assertEqual(self.commands, [(PASS, carry.DEFAULT_MAX_MINUTES)])
        self.assertTrue([a for a in actions if "room.toml unusable" in a])
        self.write("room.toml", "[checks\n")
        self.commit("break it")
        actions = []
        self.dispatcher(self.cfg(), board).main_suite_red([], actions)
        self.assertEqual(self.commands[-1][0], PASS)
        self.assertTrue([a for a in actions if "room.toml unusable" in a])

    def test_an_unchanged_room_toml_gives_the_live_command(self):
        board = self.setup_ready_pr()
        self.stub_commands()
        self.write("room.toml", f'[checks]\ntest = "{PASS}"\n')
        self.commit("add room.toml")
        self.dispatcher(self.cfg(), board).main_suite_red([], [])
        self.assertEqual(self.commands, [(PASS, carry.DEFAULT_MAX_MINUTES)])

    def test_a_project_with_ci_never_runs_the_suite_on_main(self):
        board = self.setup_ready_pr()
        d = self.dispatcher(self.cfg(ci=True), board)
        d.housekeeping()
        self.assertEqual((self.calls, board.created), ([], []))

    def test_a_busy_or_unfinished_run_holds_nothing(self):
        board = self.setup_ready_pr()
        d = self.dispatcher(self.cfg(), board)
        self.results = [("BUSY", "the suite lock is held by another run")]
        self.assertFalse(d.main_suite_red([], []))
        self.results = [("INCOMPLETE", "still running")]
        self.assertFalse(d.main_suite_red([], []))
        self.assertFalse(d.main_suite_red([], []))         # recorded: not run again at this sha
        self.assertEqual(len(self.calls), 2)


for _name in [n for n in dir(Carry) if n.startswith("test_")]:
    setattr(MainSuite, _name, None)   # MainSuite builds on Carry's repo helpers, not its tests


class DirectorDecided(Carry):
    """commonroom#306: PR #300 got CHANGES twice for a flaky test on main, was escalated, and the director approved it.
    The room then runs the suite in code and merges on that approval, with no reviewer or builder round."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self._run = carry.run_suite
        carry.run_suite = self.stub
        self.result = ("PASS", "Ran 3 tests in 0.1s OK")

    def tearDown(self):
        carry.run_suite = self._run
        super().tearDown()

    def stub(self, root, head, command, worktree, lock, max_minutes, **kw):
        self.calls.append(head)
        return self.result

    def decided(self, head, stamp="head", labels=("status:blocked", "needs:director"), behind=0, director=True):
        changes = [{"createdAt": f"2026-11-20T1{i}:00:00Z", "body": "[agent:reviewer] REVIEW: CHANGES\n\nTESTS: FAIL x"}
                   for i in (4, 5)]
        board = self.board(head)
        detail = board.details[222]
        detail["comments"] = changes
        if director:
            sha = head if stamp == "head" else stamp
            detail["comments"].append({"createdAt": "2026-11-20T16:00:00Z",
                                       "body": f"[agent:director] REVIEW: APPROVE\n\nHEAD: {sha[:7]}"})
        board.issues = [{"number": 222, "state": "OPEN", "title": "t", "milestone": {"title": "M"}, "comments": [],
                         "labels": [{"name": l} for l in labels]}]
        board.behind[head] = behind
        self.issue_comments = lambda: [b for n, b in board.posted if n == 222]
        return board

    def test_the_300_shape_runs_the_suite_once_and_merges_with_no_reviewer_or_builder(self):
        head = self.merge_main()
        board = self.decided(head)
        d = self.dispatcher(self.cfg(), board)
        self.assertFalse(d.review_wanted(board.details[222], board.issues[0]))
        d.housekeeping()
        self.assertEqual((board.merged, self.calls), ([222], [head]))
        self.assertTrue(board.posted[0][1].startswith(f"[room] TESTS: PASS {head} {PASS}: "), board.posted[0])
        self.assertIn("merged on the director's decision", self.issue_comments()[-1])
        self.assertEqual(board.labels, [])                                # no relabel, no builder round

    def test_behind_main_it_updates_first_then_runs_the_suite_at_the_carried_head(self):
        head = self.merge_main()
        board = self.decided(head, behind=1)
        d = self.dispatcher(self.cfg(), board)
        d.housekeeping()
        self.assertEqual((board.updated, board.merged, self.calls), ([222], [], []))
        new = self.merge_main("more.md", "more\n")
        detail = board.details[222]
        detail["commits"].append({"oid": new, "committedDate": "2026-11-20T17:00:00Z"})
        detail["headRefOid"] = new
        board.compare_since = lambda base, h: {"commits": [{"sha": new, "parents": [head, "m" * 40]}], "files": ["more.md"]}
        board.behind[new] = 0
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual((board.merged, self.calls), ([222], [new]))

    def test_a_merge_that_fails_after_a_pass_is_retried_next_pass_without_a_second_run(self):
        head = self.merge_main()
        board = self.decided(head)
        real, failures = board.merge, [1]

        def flaky(n):
            if failures:
                failures.pop()
                raise RuntimeError("transient")
            real(n)
        board.merge = flaky
        try:
            self.dispatcher(self.cfg(), board).housekeeping()
        except RuntimeError:
            pass
        self.assertEqual((board.merged, self.calls), ([], [head]))
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual((board.merged, self.calls), ([222], [head]))

    def test_a_failed_run_does_not_merge_comments_once_and_is_not_rerun(self):
        head = self.merge_main()
        board = self.decided(head)
        self.result = ("FAIL", "exit 1: FAILED (errors=1)")
        for _ in range(2):
            self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual((board.merged, self.calls, board.labels), ([], [head], []))
        pr_comment, issue_comment = self.issue_comments()    # the PR's result, then the issue's note (same number here)
        self.assertTrue(pr_comment.startswith(f"[room] TESTS: FAIL {head}"))
        self.assertIn("stays with the director", issue_comment)

    def test_no_director_approval_or_a_stale_one_changes_nothing(self):
        head = self.merge_main()
        for name, board in {"none": self.decided(head, director=False),
                            "older head": self.decided(head, stamp="a" * 40)}.items():
            with self.subTest(name):
                board.compare_since = lambda base, h: {"commits": [{"sha": h, "parents": ["b" * 40]}], "files": []}
                self.dispatcher(self.cfg(), board).housekeeping()
                self.assertEqual((board.merged, self.calls, board.updated, board.posted), ([], [], [], []))

    def test_an_approval_on_an_issue_that_is_not_escalated_changes_nothing(self):
        head = self.merge_main()
        board = self.decided(head, labels=("status:review",))
        self.dispatcher(self.cfg(), board).housekeeping()
        self.assertEqual((board.merged, self.calls), ([], []))

    def test_a_project_with_ci_takes_the_directors_approval_for_the_reviewers(self):
        head = self.merge_main()
        board = self.decided(head)
        board.details[222]["statusCheckRollup"] = [{"name": "t", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.dispatcher(self.cfg(ci=True), board).housekeeping()
        self.assertEqual((board.merged, self.calls), ([222], []))


for _name in [n for n in dir(Carry) if n.startswith("test_")]:
    setattr(DirectorDecided, _name, getattr(DirectorDecided, _name) if _name in DirectorDecided.__dict__ else None)
