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


class DirectorSeesHeldItems(unittest.TestCase):
    """commonroom#471: a ready item the dispatcher holds for a person is not "ready" to the director's trigger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def trigger(self, issues, approve=None):
        cfg = cfg_with(self.tmp.name)
        if approve:
            cfg["goal"]["approve"] = approve
        self.board = Board(issues)
        return dispatch.Dispatcher(cfg, self.ledger, board=self.board, log=lambda *_: None).director_trigger()

    def cap(self, body="", extra=()):
        return issue(10, READY + ["kind:capability", *extra], body=body)

    def test_unscouted_capability_names_the_issue_and_hold(self):
        reason = self.trigger([self.cap()])
        self.assertIn("#10 is ready but held: kind:capability", reason)
        self.assertIn("Scout: #N", reason)
        self.assertNotIn("nothing is ready", reason)

    def test_a_closed_scout_or_a_waiver_clears_it(self):
        self.assertIsNone(self.trigger([self.cap("Scout: #12"), issue(12, ["role:scout"], state="CLOSED")]))
        self.assertIsNone(self.trigger([self.cap(extra=["owner:waived"])]))

    def test_unapproved_item_is_named(self):
        self.assertIn("#10 is ready but held: it waits for owner:approved", self.trigger([issue(10, READY)], approve="item"))
        self.assertIsNone(self.trigger([issue(10, READY + ["owner:approved"])], approve="item"))

    def test_area_busy_and_claimed_items_do_not_wake_the_director(self):
        self.assertIsNone(self.trigger([issue(1, READY + ["area:x"]), issue(2, ["status:review", "area:x"])]))
        self.assertIsNone(self.trigger([issue(3, READY + ["claimed:kieran"])]))

    def test_the_trigger_only_reads(self):
        self.trigger([self.cap()])
        self.assertEqual(self.board.label_calls, [])
        self.assertFalse(hasattr(self.board, "comments"))


if __name__ == "__main__":
    unittest.main()
