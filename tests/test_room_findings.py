import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dispatch
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with


ROOM = Path(__file__).resolve().parents[1] / "room.py"


class RoomFindings(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.outside = self.root / "folder with no project"
        self.outside.mkdir()
        self.claude = self.root / ".claude"
        self.claude.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {
            "COMMONROOM_HOME": str(self.root / "room-home"),
            "HOME": str(self.root), "USERPROFILE": str(self.root),
            "CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.root / ".codex"),
        }))

    def room(self, *args, code=0):
        result = subprocess.run([sys.executable, str(ROOM), "finding", *args], cwd=self.outside,
                                capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, code, result.stderr)
        return result.stdout.strip()

    def director(self, ledger, *, accepts_findings):
        cfg = cfg_with(self.root)
        cfg["director"] = {"steer_every_hours": 0, "room_findings": accepts_findings}
        return dispatch.Dispatcher(cfg, ledger, board=FakeBoard([]), log=lambda *_: None)

    def ledger(self):
        ledger = Ledger()
        self.addCleanup(ledger.close)
        return ledger

    def test_outside_project_report_wakes_only_the_room_director(self):
        self.assertEqual(self.room("A search missed the café", "--words", "“Try the other folder.”"), "Finding 1 recorded")
        rows = json.loads(self.room("--list"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cwd"], str(self.outside))
        self.assertEqual(rows[0]["detail"], "A search missed the café")
        self.assertEqual(rows[0]["words"], "“Try the other folder.”")

        ledger = self.ledger()
        director = self.director(ledger, accepts_findings=True)
        ledger.record_findings(director.project, [])
        self.assertIsNone(self.director(ledger, accepts_findings=False).director_trigger())
        reason = director.director_trigger()
        self.assertEqual(reason, "room findings awaiting triage: 1")
        run = ledger.start_run(director.project, None, "director", "claude", "opus")
        self.assertIsNone(director.director_trigger())
        ledger.db.execute("UPDATE runs SET started_at=? WHERE id=?", (time.time() - 7200, run))
        self.assertEqual(director.director_trigger(), reason)

    def test_each_disposition_is_durable_and_removes_the_trigger(self):
        for detail in ("new work", "already tracked", "working as intended"):
            self.room(detail)
        self.room("--created", "1", "52")
        self.room("--attached", "2", "18")
        self.room("--closed", "3", "Expected with the setting off")
        self.room("--closed", "1", "replacement", code=1)
        self.room("--closed", "9", "missing", code=1)

        self.assertEqual(json.loads(self.room("--list")), [])
        trail = json.loads(self.room("--trail"))
        self.assertEqual([(row["outcome"], row["issue"], row["reason"]) for row in trail],
                         [("created", 52, None), ("attached", 18, None),
                          ("closed", None, "Expected with the setting off")])
        self.assertTrue(all(row["resolved_at"] for row in trail))
        self.assertIsNone(self.director(self.ledger(), accepts_findings=True).director_trigger())

    def test_empty_detail_and_empty_close_reason_are_rejected(self):
        self.room(" ", code=1)
        self.room("valid finding")
        self.room("--closed", "1", " ", code=1)
        self.assertEqual([row["id"] for row in json.loads(self.room("--list"))], [1])
