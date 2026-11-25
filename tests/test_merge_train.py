"""commonroom#612: a merge train. Several approved, tested PRs behind main land together on one suite run, oldest
first, and main ends at the tree that was tested. Real git (the repo is its own `origin`), a real test command, a fake
board whose merge squashes the PR's branch onto main as GitHub does."""
import copy
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dispatch
from test_dispatch import owned
from test_self_serve import Base, MergeBoard, project_cfg

# Passes on a tree with main's docs.md and no bad.txt: a PR that adds bad.txt breaks the suite.
SUITE = ("python -c \"import os, sys; print('Ran 3 tests in 0.1s'); print(); print('OK'); "
         "sys.exit(0 if os.path.exists('docs.md') and not os.path.exists('bad.txt') else 1)\"")


class TrainBoard(MergeBoard):
    def __init__(self, test, prs, details, issues):
        super().__init__(prs, details, issues)
        self.test, self.posted, self.merged_heads = test, [], []

    def _git(self, *args):
        return self.test.git(*args)

    def pr_detail(self, n):
        return owned(copy.deepcopy(self.details[n]), pr=True)

    def behind_main(self, detail):
        return int(self._git("rev-list", "--count", f"{detail['headRefOid']}..main"))

    def comment(self, n, body):
        self.posted.append((n, f"{dispatch.MARK} {body}"))

    def merge(self, n, *, head=None):
        """GitHub's squash merge of a branch that contains main: one new commit on main with the branch's tree."""
        branch = self.details[n]["headRefName"]
        tip = self._git("rev-parse", f"refs/heads/{branch}")
        if head and tip != head:
            raise RuntimeError(f"Head branch was modified: {tip[:7]} is not {head[:7]}")
        if subprocess.run(["git", "merge-base", "--is-ancestor", "main", tip], cwd=self.test.root).returncode:
            raise RuntimeError("test board: the branch doesn't contain main")
        squash = self._git("commit-tree", f"{tip}^{{tree}}", "-p", "main", "-m", f"PR #{n}")
        self._git("update-ref", "refs/heads/main", squash)
        self.merged.append(n)
        self.merged_heads.append(head)


class Train(Base):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("core.autocrlf", "false")):
            self.git("config", key, value)
        self.git("remote", "add", "origin", str(self.root))      # fetching origin reads this repo's own branches
        self.write("a.txt", "one\ntwo\nthree\n")
        self.commit("base")

    def git(self, *args):
        done = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        return done.stdout.strip()

    def write(self, name, text):
        (self.root / name).write_bytes(text.encode())

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def branch(self, n, files):
        """Branch i<n>-thing off main's first commit with `files` ({name: text}); its head."""
        self.git("checkout", "-q", "-b", f"i{n}-thing", self.git("rev-list", "--max-parents=0", "main"))
        for name, text in files.items():
            self.write(name, text)
        head = self.commit(f"work for #{n}")
        self.git("checkout", "-q", "main")
        return head

    def setup_board(self, branches, labels=None, main_moves=True):
        """PR n for each {n: files}, each approved with a passing run on its own head; then main moves on, so every one
        is behind it. The checkout ends detached, so main is a ref like any other."""
        heads = {n: self.branch(n, files) for n, files in branches.items()}
        if main_moves:
            self.write("docs.md", "docs\n")
            self.commit("main moves")
        self.git("checkout", "-q", "--detach")
        prs, details, issues = [], {}, []
        for n, head in heads.items():
            names = [{"name": l} for l in (labels or {}).get(n, ())]
            prs.append({"number": n, "headRefName": f"i{n}-thing", "labels": names})
            details[n] = {"number": n, "headRefName": f"i{n}-thing", "headRefOid": head, "baseRefName": "main",
                          "mergeable": "MERGEABLE", "labels": names, "statusCheckRollup": [],
                          "commits": [{"oid": head, "committedDate": "2026-11-25T00:00:00Z"}],
                          "files": [{"path": f} for f in branches[n]],
                          "comments": [{"createdAt": "2026-11-25T01:00:00Z",
                                        "body": f"[agent:reviewer] REVIEW: APPROVE\n\nTESTS: PASS {head[:7]} {SUITE}: "
                                                f"Ran 3 tests in 0.1s OK"}]}
            issues.append({"number": n, "state": "OPEN", "title": f"thing {n}", "labels": [{"name": "status:review"}],
                           "comments": [], "body": ""})
        self.heads = heads
        return TrainBoard(self, prs, details, issues)

    def cfg(self, carry=True):
        cfg = project_cfg(self.root)
        cfg["checks"].update({"test": SUITE})
        cfg["project"]["worktrees"] = str(Path(self.tmp.name) / "wt")
        cfg["merge"] = {"carry_tested_approval": True} if carry else {}
        return cfg

    def run_pass(self, board, carry=True, red=False):
        d = self.dispatcher(self.cfg(carry), board)
        with mock.patch.object(dispatch.Dispatcher, "main_red", lambda *a: red):
            return d, d.housekeeping()

    def carry_rows(self):
        return [r for r in self.ledger.recent_runs("commonroom", limit=50) if r["role"] == "carry"]

    def files_on_main(self):
        return set(self.git("ls-tree", "--name-only", "main").split())

    def test_three_approved_prs_behind_main_land_on_one_suite_run_oldest_first(self):
        board = self.setup_board({1: {"one.txt": "1\n"}, 2: {"two.txt": "2\n"}, 3: {"three.txt": "3\n"}})
        _, actions = self.run_pass(board)
        self.assertEqual(board.merged, [1, 2, 3], actions)
        (row,) = self.carry_rows()                                       # one suite run, for the whole train
        out = row["outputs"][0]
        self.assertEqual((row["provider"], out["result"]), ("none", "PASS"))
        self.assertEqual([m["pr"] for m in out["train"]], [1, 2, 3])
        tip = out["head"]
        self.assertEqual(self.git("rev-parse", "main^{tree}"), self.git("rev-parse", f"{tip}^{{tree}}"))   # main is the tested tree
        self.assertEqual(self.files_on_main(), {"a.txt", "docs.md", "one.txt", "two.txt", "three.txt"})
        self.assertEqual([n for n, _ in board.posted], [1, 2, 3])
        for _, body in board.posted:
            self.assertTrue(body.startswith(f"[room] TESTS: PASS {tip} {SUITE}: Ran 3 tests in 0.1s OK"), body)
            self.assertIn("PRs #1, #2, #3", body)
        for n, head in zip(board.merged, board.merged_heads):          # each merged exactly the commit pushed to it
            self.assertEqual(self.git("rev-parse", f"refs/heads/i{n}-thing"), head)
            self.assertEqual(self.git("rev-parse", f"{head}^1"), self.heads[n])   # its own head first, then main
        self.assertEqual(len([r for r in self.ledger.recent_runs("commonroom", limit=50) if r["role"] == "merge"]), 3)
        self.assertEqual(board.updated, [])
        self.assertFalse((Path(self.tmp.name) / "wt" / "carry-train").exists())

    def test_a_pr_that_conflicts_in_the_train_is_left_out_and_goes_back_for_a_merge_main(self):
        board = self.setup_board({1: {"a.txt": "one\nTWO by 1\nthree\n"}, 2: {"a.txt": "one\nTWO by 2\nthree\n"},
                                  3: {"three.txt": "3\n"}})
        _, actions = self.run_pass(board)
        self.assertEqual(board.merged, [1, 3], actions)
        (row,) = self.carry_rows()
        self.assertEqual([m["pr"] for m in row["outputs"][0]["train"]], [1, 3])
        self.assertIn((2, ("status:ready", "needs:changes"), ("status:review", "status:in-progress")), board.labels)
        self.assertTrue(any("PR #2 has merge conflicts" in a for a in actions), actions)
        self.assertEqual(self.git("rev-parse", "refs/heads/i2-thing"), self.heads[2])   # its branch is untouched

    def test_a_failing_train_merges_nothing_and_its_prs_go_one_at_a_time(self):
        board = self.setup_board({1: {"one.txt": "1\n"}, 2: {"bad.txt": "x\n"}, 3: {"three.txt": "3\n"}})
        _, actions = self.run_pass(board)
        self.assertEqual(board.merged, [])
        (row,) = self.carry_rows()
        self.assertEqual(row["outputs"][0]["result"], "FAIL")
        self.assertEqual([n for n, _ in board.posted], [1, 2, 3])
        for _, body in board.posted:
            self.assertIn("so nothing merged. Each PR goes through the one-at-a-time path", body)
        for n, head in self.heads.items():
            self.assertEqual(self.git("rev-parse", f"refs/heads/i{n}-thing"), head)   # no branch was pushed to
        self.assertEqual(board.updated, [1])           # today's path: the oldest is brought up to date with main
        # The next pass builds no train from those heads: they stay on the one-at-a-time path.
        _, actions = self.run_pass(board)
        self.assertEqual((len(self.carry_rows()), board.merged), (1, []), actions)
        self.assertFalse(any("merge train" in a for a in actions), actions)

    def test_a_single_approved_pr_behind_main_takes_todays_path(self):
        board = self.setup_board({1: {"one.txt": "1\n"}})
        _, actions = self.run_pass(board)
        self.assertEqual((board.merged, board.updated, board.posted, self.carry_rows()), ([], [1], [], []))
        self.assertEqual(self.git("rev-parse", "refs/heads/i1-thing"), self.heads[1])

    def test_without_carrying_nothing_changes(self):
        board = self.setup_board({1: {"one.txt": "1\n"}, 2: {"two.txt": "2\n"}})
        self.run_pass(board, carry=False)
        self.assertEqual((board.merged, board.updated, board.posted, self.carry_rows()), ([], [1], [], []))

    def test_a_pr_waiting_on_the_director_is_never_in_a_train(self):
        board = self.setup_board({1: {"one.txt": "1\n"}, 2: {"two.txt": "2\n"}, 3: {"three.txt": "3\n"}},
                                 labels={2: ["needs:director"]})
        _, actions = self.run_pass(board)
        self.assertEqual(board.merged, [1, 3], actions)
        (row,) = self.carry_rows()
        self.assertEqual([m["pr"] for m in row["outputs"][0]["train"]], [1, 3])
        self.assertEqual(self.git("rev-parse", "refs/heads/i2-thing"), self.heads[2])

    def test_an_escalated_issue_is_never_in_a_train(self):
        board = self.setup_board({1: {"one.txt": "1\n"}, 2: {"two.txt": "2\n"}})
        board.issues[1]["labels"].append({"name": "needs:director"})
        self.run_pass(board)
        self.assertEqual((board.merged, self.carry_rows()), ([], []))

    def test_a_red_main_gets_no_train(self):
        board = self.setup_board({1: {"one.txt": "1\n"}, 2: {"two.txt": "2\n"}})
        self.run_pass(board, red=True)
        self.assertEqual((board.merged, board.updated, board.posted, self.carry_rows()), ([], [], [], []))

    def test_landing_stops_when_main_moved_under_the_train(self):
        board = self.setup_board({1: {"one.txt": "1\n"}, 2: {"two.txt": "2\n"}})
        squash = TrainBoard.merge

        def merge_then_someone_else_lands(b, n, *, head=None):
            squash(b, n, head=head)
            self.write_commit_on_main("other.txt")

        with mock.patch.object(TrainBoard, "merge", merge_then_someone_else_lands):
            _, actions = self.run_pass(board)
        self.assertEqual(board.merged, [1])
        self.assertTrue(any("merge train stopped before PR #2" in a for a in actions), actions)
        self.assertEqual(self.git("rev-parse", "refs/heads/i2-thing"), self.heads[2])

    def write_commit_on_main(self, name):
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.root, input="x\n", capture_output=True,
                              text=True).stdout.strip()
        tree = subprocess.run(["git", "mktree"], cwd=self.root, capture_output=True, text=True,
                              input=self.git("ls-tree", "main") + f"\n100644 blob {blob}\t{name}\n").stdout.strip()
        self.git("update-ref", "refs/heads/main", self.git("commit-tree", tree, "-p", "main", "-m", "other"))


if __name__ == "__main__":
    unittest.main()
