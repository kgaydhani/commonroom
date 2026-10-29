import tempfile
import unittest
from pathlib import Path

from commonroom import dispatch
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with, issue

READY = ["status:ready", "role:builder"]


class AreaSerialised(unittest.TestCase):
    """commonroom#161: issues sharing an `area:*` label are built one at a time, as `risk:golden` is."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def ready(self, issues):
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard(issues), log=lambda *_: None)
        return [i["number"] for i in d.ready_builds(issues)]

    def test_a_held_area_hides_its_ready_issues_but_not_other_areas_or_unlabelled(self):
        issues = [issue(1, ["status:in-progress", "role:builder", "area:http"]), issue(2, READY + ["area:http"]),
                  issue(3, READY + ["area:ui"]), issue(4, READY)]
        self.assertEqual(self.ready(issues), [3, 4])

    def test_with_none_held_only_the_top_issue_per_area_is_offered(self):
        issues = [issue(1, READY + ["area:http", "prio:low"]), issue(2, READY + ["area:http", "prio:high"])]
        self.assertEqual(self.ready(issues), [2])

    def test_two_areas_are_held_back_if_either_is_held(self):
        issues = [issue(1, ["status:review", "role:builder", "area:ui"]), issue(2, READY + ["area:http", "area:ui"]),
                  issue(3, READY + ["area:http"])]
        self.assertEqual(self.ready(issues), [3])

    def test_golden_counts_as_an_area(self):
        issues = [issue(1, ["status:review", "role:builder", "risk:golden"]), issue(2, READY + ["risk:golden"])]
        self.assertEqual(self.ready(issues), [])

    def test_no_area_labels_changes_nothing(self):
        issues = [issue(1, READY), issue(2, READY), issue(3, ["status:in-progress", "role:builder"])]
        self.assertEqual(self.ready(issues), [1, 2])


if __name__ == "__main__":
    unittest.main()
