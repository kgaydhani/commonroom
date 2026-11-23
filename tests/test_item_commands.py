"""commonroom#477: `room comment`, `room label` and `room close` on a local board, and the digest's approval line there."""
import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import board, cli, handshake, registry
from commonroom.localboard import LocalBoard


class ItemCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "alpha"
        self.board = LocalBoard(self.folder)
        self.n = self.board.create_issue("Write the intro", "b", labels=["status:ready", "role:builder"])
        self.project = registry.Project("alpha", self.tmp.name, 1.0)
        self.toml = {"project": {"board": "local"}, "goal": {"approve": "item"}}
        for target, value in (("_start", (None, self.project)), ("room_toml", self.toml), ("folder_of", self.folder)):
            patcher = mock.patch.object(handshake, target, return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(board, "gh", side_effect=AssertionError("gh called"))
        patcher.start(); self.addCleanup(patcher.stop)

    def run_cmd(self, fn, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(argparse.Namespace(name="alpha", project=None, **kw))
        return code, out.getvalue(), err.getvalue()

    def item(self):
        return next(i for i in self.board.all_issues() if i["number"] == self.n)

    def test_comment_is_the_owners_and_unmarked(self):
        self.assertEqual(self.run_cmd(cli.cmd_comment, n=self.n, text="yes, go")[0], 0)
        c = self.item()["comments"][-1]
        self.assertEqual((c["body"], c["author"]["login"]), ("yes, go", "owner"))

    def test_comment_starting_with_bracket_gets_zero_width_prefix(self):
        self.run_cmd(cli.cmd_comment, n=self.n, text="[x] done")
        self.assertEqual(self.item()["comments"][-1]["body"], "​[x] done")

    def test_label_add_and_remove(self):
        self.run_cmd(cli.cmd_label, n=self.n, add=["owner:approved"], remove=["status:ready"])
        names = {l["name"] for l in self.item()["labels"]}
        self.assertIn("owner:approved", names)
        self.assertNotIn("status:ready", names)
        self.assertEqual(self.run_cmd(cli.cmd_label, n=self.n, add=None, remove=None)[0], 2)

    def test_close_with_reason(self):
        self.assertEqual(self.run_cmd(cli.cmd_close, n=self.n, reason="not needed")[0], 0)
        i = self.item()
        self.assertEqual(i["state"], "CLOSED")
        self.assertEqual(i["comments"][-1]["body"], "not needed")

    def test_unknown_item_is_refused(self):
        for fn, kw in ((cli.cmd_comment, {"text": "x"}), (cli.cmd_label, {"add": ["a"], "remove": None}), (cli.cmd_close, {"reason": ""})):
            code, _, err = self.run_cmd(fn, n=99, **kw)
            self.assertEqual(code, 1)
            self.assertIn("no item #99", err)

    def github_fake(self, state):
        calls = []
        class Fake:
            def __init__(self, repo): pass
            def all_issues(self): raise AssertionError("all_issues called")
            def issue_state(self, n): return state
            def ensure_label(self, name): calls.append(("ensure", name))
            def set_labels(self, n, add=(), remove=()): calls.append(("labels", n, list(add)))
            def owner_comment(self, n, text): calls.append(("comment", n, text))
            def owner_close(self, n, text): calls.append(("close", n, text))
        self.toml["project"] = {"repo": "o/r"}
        return Fake, calls

    def test_github_item_is_checked_by_issue_state_not_the_listing(self):
        fake, calls = self.github_fake("OPEN")
        code, _, _ = self.run_cmd(lambda a: cli.cmd_label(a, board_for=fake), n=9999, add=["x"], remove=None)
        self.assertEqual(code, 0)
        self.assertIn(("labels", 9999, ["x"]), calls)

    def test_github_unreadable_item_is_refused_with_no_write(self):
        fake, calls = self.github_fake(None)
        for fn, kw in ((cli.cmd_comment, {"text": "x"}), (cli.cmd_label, {"add": ["a"], "remove": None}), (cli.cmd_close, {"reason": ""})):
            code, _, err = self.run_cmd(lambda a, fn=fn: fn(a, board_for=fake), n=9999, **kw)
            self.assertEqual(code, 1)
            self.assertIn("has no item #9999", err)
        self.assertEqual(calls, [])

    def test_unknown_project_is_refused(self):
        with mock.patch.object(handshake, "_start", side_effect=handshake.HandshakeError("refused: no project")):
            code, _, err = self.run_cmd(cli.cmd_close, n=self.n, reason="")
        self.assertEqual(code, 1)
        self.assertIn("refused", err)

    def test_digest_approval_line_names_room_label(self):
        line = handshake._approval(self.project)
        self.assertIn(f"room label alpha {self.n} --add owner:approved", line)
        self.assertNotIn("gh issue edit", line)
        self.board.set_labels(self.n, add=["owner:approved"])
        self.assertEqual(handshake._approval(self.project), "")


if __name__ == "__main__":
    unittest.main()
