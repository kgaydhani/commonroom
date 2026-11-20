"""commonroom#226: an approval carries across a clean merge of main once the room has run the tests on the new head.
Real git and a real (fake) test command; a fake board."""
import subprocess
import unittest
from pathlib import Path

from commonroom import carry, dispatch
from commonroom.lock import claim_lock, release_lock, suite_lock
from test_self_serve import Base, MergeBoard, project_cfg

PASS = "python -c \"import os, sys; print('Ran 3 tests in 0.1s'); print(); print('OK'); sys.exit(0 if os.path.exists('docs.md') and os.path.exists('f.txt') else 1)\""
FAIL = "python -c \"import sys; print('FAILED (errors=1)'); sys.exit(1)\""
HANG = "python -c \"import time; time.sleep(60)\""
BRANCH = "i222-docs"


class Board(MergeBoard):
    def __init__(self, *a):
        super().__init__(*a)
        self.posted = []

    def comment(self, n, body):
        self.posted.append((n, body))


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
                self.assertTrue(d.needs_review(board.details[222]))    # a model reviews it, as before
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

    def test_a_director_path_whose_stamp_cannot_be_carried_carries_nothing(self):
        board = self.board(self.merge_main(), files=["room.toml"])
        cfg = self.cfg()
        cfg["merge"]["director_paths"] = ["room.toml"]
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

    def test_the_comment_counts_only_from_a_trusted_author_after_the_head_it_names(self):
        head = self.merge_main()
        good = carry.comment("PASS", head, PASS, "Ran 3 tests OK", "2026-11-20T13:00:00Z", self.tested)
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
        self.assertEqual(carry.parse(body), {"result": "PASS", "head": "b" * 40, "line": "python -m unittest: Ran 1 test OK",
                                             "approval_at": "2026-11-20T13:00:00Z", "tested": "a" * 40})
        self.assertIsNone(carry.parse("[agent:builder] quoting it: " + body))
        self.assertEqual(carry.parse(carry.comment("FAIL", "b" * 40, "x", "exit 1", "t", "a" * 40))["approval_at"], "")
        self.assertEqual(carry.summary("....\n----\nRan 524 tests in 80.163s\n\nOK\n"), "Ran 524 tests in 80.163s OK")


if __name__ == "__main__":
    unittest.main()
