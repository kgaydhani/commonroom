"""commonroom#456: `room items` and `room claim` on a local board, and the dispatcher leaves a claimed item alone."""
import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, dispatch, handshake, registry
from commonroom.ledger import Ledger
from commonroom.localboard import LocalBoard

from test_dispatch import FakeBoard, cfg_with, issue


class ItemsAndClaim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "alpha"
        self.board = LocalBoard(self.folder)
        self.a = self.board.create_issue("Write the intro", "b", labels=["status:ready", "role:writer", "size:S", "prio:top"])
        self.b = self.board.create_issue("Old chore", "b", labels=["status:ready"])
        self.board.close_issue(self.b, "done")
        self.project = registry.Project("alpha", self.tmp.name, 1.0)
        for target, value in (("_start", (None, self.project)), ("room_toml", {"project": {"board": "local"}}),
                              ("folder_of", self.folder)):
            patcher = mock.patch.object(handshake, target, return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)

    def run_cmd(self, fn, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(argparse.Namespace(name="alpha", project=None, **kw))
        return code, out.getvalue(), err.getvalue()

    def test_items_lists_open_items_and_all_adds_closed(self):
        code, out, _ = self.run_cmd(cli.cmd_items, all=False)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"#{self.a} role:writer size:S status:ready Write the intro")
        _, out, _ = self.run_cmd(cli.cmd_items, all=True)
        self.assertIn("Old chore [closed]", out)
        self.assertIn("Write the intro", out)

    def test_claim_adds_label_and_comment_and_release_removes_it(self):
        code, out, _ = self.run_cmd(cli.cmd_claim, n=self.a, who="owner", release=False)
        self.assertEqual(code, 0)
        item = self.board.open_issues(None)[0]
        self.assertIn("claimed:owner", dispatch.labels(item))
        self.assertIn("claimed by owner", item["comments"][-1]["body"])
        self.run_cmd(cli.cmd_claim, n=self.a, who="owner", release=True)
        item = self.board.open_issues(None)[0]
        self.assertNotIn("claimed:owner", dispatch.labels(item))
        self.assertIn("released", item["comments"][-1]["body"])

    def test_claim_of_a_missing_item_is_refused(self):
        code, _, err = self.run_cmd(cli.cmd_claim, n=99, who="owner", release=False)
        self.assertEqual(code, 1)
        self.assertIn("no item #99", err)

    def test_dispatcher_skips_a_claimed_ready_item_until_released(self):
        ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        self.addCleanup(ledger.close)
        base = [issue(1, ["status:ready", "role:builder"]), issue(2, ["status:ready", "role:builder", "claimed:owner"])]

        def picked(issues):
            d = dispatch.Dispatcher(cfg_with(self.tmp.name), ledger, board=FakeBoard(issues), log=lambda *_: None)
            return [i["number"] for i in d.ready_builds(issues)]
        self.assertEqual(picked(base), [1])
        self.assertEqual(picked([base[1]]), [])
        self.assertEqual(picked([issue(2, ["status:ready", "role:builder"])]), [2])

    def test_an_unclaimed_item_is_not_affected(self):
        self.assertFalse(dispatch.claimed({"status:ready", "role:builder", "claim:external"}))
        self.assertTrue(dispatch.claimed({"claimed:kieran"}))


if __name__ == "__main__":
    unittest.main()
