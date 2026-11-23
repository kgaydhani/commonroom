import tempfile
import unittest
from pathlib import Path

from commonroom import config, dispatch
from commonroom.ledger import Ledger

from test_dispatch import cfg_with, issue
from test_scout_check import Board, READY


class ScoutOf(unittest.TestCase):
    def test_scout_line(self):
        self.assertEqual(config.scout_of("Why\nScout: #12\n"), 12)
        self.assertEqual(config.scout_of("Why\r\nScout: #12\r\nmore"), 12)
        self.assertIsNone(config.scout_of("see Scout: #12 later"))
        self.assertIsNone(config.scout_of(None))


class CapabilityHold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        self.logs = []

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def picked(self, issues):
        board = Board(issues)
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=board, log=self.logs.append)
        return [i["number"] for i in d.ready_builds(board.open_issues(None))]

    def cap(self, body="", extra=()):
        return issue(10, READY + ["kind:capability", *extra], body=body)

    def test_no_scout_line_is_held_and_logged(self):
        self.assertEqual(self.picked([self.cap()]), [])
        self.assertTrue(any("#10 held" in m and "Scout" in m for m in self.logs))

    def test_open_scout_holds(self):
        self.assertEqual(self.picked([self.cap("Scout: #12"), issue(12, ["role:scout"])]), [])

    def test_closed_scout_starts(self):
        self.assertEqual(self.picked([self.cap("Scout: #12"), issue(12, ["role:scout"], state="CLOSED")]), [10])

    def test_waived_starts(self):
        self.assertEqual(self.picked([self.cap(extra=["owner:waived"])]), [10])

    def test_other_kinds_are_unchanged(self):
        self.assertEqual(self.picked([issue(10, READY + ["kind:feature"])]), [10])
        self.assertEqual(self.logs, [])


if __name__ == "__main__":
    unittest.main()
