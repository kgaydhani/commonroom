import sys
import tempfile
import unittest
from pathlib import Path

from commonroom import services, steward
from commonroom.ledger import Ledger


class HeartbeatFailing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")
        self.addCleanup(self.ledger.close)

    def cfg(self, code):
        (Path(self.tmp.name) / "code.txt").write_text(str(code))   # the same command line exits differently
        return {"_root": self.tmp.name, "project": {"name": "demo"}, "heartbeat": {"commands": [
            ["{python}", "-c", "import sys; sys.exit(int(open('code.txt').read()))"]]}}

    def found(self):
        return steward.heartbeat_failing(self.ledger.heartbeat_runs("demo"), 3)

    def test_three_failures_raise_two_do_not_success_clears(self):
        for _ in range(2):
            services.tick(self.cfg(7), ledger=self.ledger)
        self.assertEqual(self.found(), [])
        services.tick(self.cfg(7), ledger=self.ledger)
        (f,) = self.found()
        self.assertEqual(f["invariant"], "heartbeat-failing")
        self.assertIn("sys.exit(int(", f["detail"])
        self.assertIn("last exit code 7", f["detail"])
        services.tick(self.cfg(0), ledger=self.ledger)
        self.assertEqual(self.found(), [])

    def test_unstartable_command_counts_as_failure(self):
        cfg = {"_root": self.tmp.name, "project": {"name": "demo"}, "heartbeat": {"commands": [["no-such-tool-xyz"]]}}
        for _ in range(3):
            services.tick(cfg, ledger=self.ledger)
        self.assertEqual(len(self.found()), 1)

    def test_tick_without_ledger_records_nothing(self):
        services.tick(self.cfg(1))
        self.assertEqual(self.ledger.heartbeat_runs("demo"), {})

    def test_projects_are_separate_and_history_is_trimmed(self):
        for _ in range(30):
            self.ledger.record_heartbeat("demo", "x", 1)
        self.assertEqual(len(self.ledger.heartbeat_runs("demo")["x"]), Ledger.HEARTBEAT_KEEP)
        self.assertEqual(self.ledger.heartbeat_runs("other"), {})


if __name__ == "__main__":
    unittest.main()
