"""commonroom#454: LocalBoard keeps GitHubBoard's issue methods in files, and never rewrites or deletes."""
import hashlib
import inspect
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from commonroom.dispatch import GitHubBoard
from commonroom.localboard import LocalBoard

from test_board_contract import shape

METHODS = ["open_issues", "all_issues", "create_issue", "comment", "set_labels", "close_issue", "issue_state",
           "issue_comments", "edit_issue", "set_milestone", "ensure_label"]
KEYS = "number,title,labels,assignees,milestone,updatedAt,state,body,comments,author".split(",")


def hashes(folder):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(folder).rglob("*") if p.is_file()}


def worker(folder, who, item):
    board = LocalBoard(folder)
    made = [board.create_issue(f"{who}{i}", "b") for i in range(50)]
    for i in range(20):
        board.comment(item, f"{who}{i}")
    return made


class Contract(unittest.TestCase):
    def test_every_issue_method_has_githubboards_call_shape(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(LocalBoard, name, None)))
                self.assertEqual(shape(getattr(LocalBoard, name)), shape(getattr(GitHubBoard, name)))
                self.assertEqual(list(inspect.signature(getattr(LocalBoard, name)).parameters),
                                 list(inspect.signature(getattr(GitHubBoard, name)).parameters))


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = self._tmp.name
        self.board = LocalBoard(self.folder)

    def test_create_label_comment_close(self):
        n = self.board.create_issue("Paint", "a body", labels=["art"], milestone="M1", assignees=["kg"])
        self.board.ensure_label("ready")
        self.board.set_labels(n, add=["ready"], remove=["art"])
        self.board.comment(n, "hello")
        issue = self.board.open_issues("M1")[0]
        self.assertEqual(set(KEYS) - set(issue), set())
        self.assertEqual(issue["labels"], [{"name": "ready"}])
        self.assertEqual(issue["assignees"], [{"login": "kg"}])
        self.assertEqual(issue["milestone"], {"title": "M1"})
        self.assertEqual(issue["comments"][0]["body"], "[room] hello")
        self.assertEqual(set(issue["comments"][0]), {"createdAt", "body", "author"})
        self.assertEqual(issue["comments"][0]["author"], {"login": "room"})
        self.assertEqual((issue["state"], self.board.issue_state(n)), ("OPEN", "OPEN"))
        self.board.close_issue(n, "done")
        self.assertEqual(self.board.issue_state(n), "CLOSED")
        self.assertEqual(self.board.open_issues("M1"), [])
        self.assertEqual(len(self.board.all_issues()[0]["comments"]), 2)
        self.assertIsNone(self.board.issue_state(99))

    def test_open_issues_filters_by_milestone(self):
        a = self.board.create_issue("a", "", milestone="M1")
        b = self.board.create_issue("b", "")
        self.board.set_milestone(b, "M2")
        self.assertEqual([i["number"] for i in self.board.open_issues("M1")], [a])
        self.assertEqual([i["number"] for i in self.board.open_issues("M2")], [b])
        self.assertEqual([i["number"] for i in self.board.open_issues(None)], [a, b])

    def test_edit_keeps_the_old_text_on_disk(self):
        n = self.board.create_issue("old", "old body")
        before = hashes(self.folder)
        self.board.edit_issue(n, "new", "new body")
        self.assertEqual(self.board.all_issues()[0]["title"], "new")
        after = hashes(self.folder)
        self.assertTrue(all(after.get(k) == v for k, v in before.items()))   # nothing present before changed or went

    def test_issue_comments_since(self):
        n = self.board.create_issue("a", "")
        self.board.comment(n, "one")
        self.assertEqual(len(self.board.issue_comments(n, 0)), 1)
        self.assertEqual(self.board.issue_comments(n, 9e12), [])

    def test_two_boards_alternate_to_distinct_numbers(self):
        a, b = LocalBoard(self.folder), LocalBoard(self.folder)
        numbers = [x.create_issue("t", "") for _ in range(5) for x in (a, b)]
        self.assertEqual(sorted(numbers), list(range(1, 11)))

    def test_unfinished_and_broken_files_are_skipped(self):
        n = self.board.create_issue("a", "")
        events = self.board.items / str(n) / "events"
        events.mkdir(exist_ok=True)
        (events / "1-1-x.json.tmp").write_text("{", encoding="utf-8")
        (events / "2-1-y.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.board.all_issues()[0]["comments"], [])

    def test_comment_on_a_missing_item_raises(self):
        with self.assertRaises(RuntimeError):
            self.board.comment(5, "x")


class TwoProcesses(unittest.TestCase):
    def test_two_processes_make_100_distinct_numbers_and_lose_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            item = LocalBoard(folder).create_issue("shared", "")
            before = hashes(folder)
            with multiprocessing.get_context("spawn").Pool(2) as pool:
                got = pool.starmap(worker, [(folder, "a", item), (folder, "b", item)])
            numbers = got[0] + got[1]
            self.assertEqual(len(set(numbers)), 100)
            comments = [c["body"] for c in LocalBoard(folder).all_issues()[0]["comments"]]
            self.assertEqual(len(comments), 40)
            self.assertEqual({f"[room] {w}{i}" for w in "ab" for i in range(20)}, set(comments))
            after = hashes(folder)
            self.assertTrue(all(after.get(k) == v for k, v in before.items()))


if __name__ == "__main__":
    unittest.main()
