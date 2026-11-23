"""commonroom#324: a project's optional [measure] command is read on a cadence and shown in the digest.
Fake commands, temporary ledger and home."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import handshake, measure, registry, trials
from commonroom.ledger import Ledger

CFG = {"measure": {"command": "count", "every_hours": 2, "label": "followers"}}
NOW = 1_800_000_000


class Measure(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(Path(tmp.name) / "home")})
        (Path(tmp.name) / "trials.toml").write_text("", encoding="utf-8")   # no trial in the digest
        trial_file = mock.patch.object(trials, "FILE", Path(tmp.name) / "trials.toml")
        trial_file.start()
        self.addCleanup(trial_file.stop)
        env.start()
        self.addCleanup(env.stop)
        folder = Path(tmp.name) / "alpha"
        folder.mkdir()
        (folder / "room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
        self.folder = folder
        self.reg = registry.Registry()
        self.reg.projects["alpha"] = registry.Project("alpha", str(folder), 30)
        self.ledger = Ledger(Path(tmp.name) / "room.sqlite3")
        self.addCleanup(self.ledger.close)
        self.calls = []

    def fake(self, code=0, out="42\nmore\n"):
        def run(command, cwd, bash):
            self.calls.append(command)
            return code, out
        return run

    def take(self, at, run=None, cfg=CFG):
        return measure.take(self.ledger, "alpha", cfg, self.folder, now=at, run=run or self.fake())

    def test_reading_recorded_and_cadence(self):
        got = self.take(NOW)
        self.assertEqual((got["exit_code"], got["line"]), (0, "42"))
        self.assertEqual(self.ledger.readings("alpha")[0]["line"], "42")
        self.assertIsNone(self.take(NOW + 3600))
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(self.take(NOW + 7200))
        self.assertEqual(len(self.calls), 2)

    def test_no_measure_runs_nothing(self):
        self.assertIsNone(self.take(NOW, cfg={}))
        self.assertEqual((self.calls, self.ledger.readings("alpha")), ([], []))

    def test_line_trimmed(self):
        self.assertEqual(len(self.take(NOW, self.fake(out="x" * 500))["line"]), 200)

    def test_failed_and_timed_out_are_recorded(self):
        self.assertEqual(self.take(NOW, self.fake(code=3, out=""))["exit_code"], 3)

        def slow(command, cwd, bash):
            raise subprocess.TimeoutExpired(command, 60)
        got = self.take(NOW + 10 * 3600, slow)
        self.assertNotEqual(got["exit_code"], 0)
        self.assertIn("timed out", got["line"])

        def broken(command, cwd, bash):
            raise OSError("nope")
        self.assertNotEqual(self.take(NOW + 20 * 3600, broken)["exit_code"], 0)
        self.assertEqual(len(self.ledger.readings("alpha")), 3)

    def test_real_command_runs(self):
        got = measure.take(self.ledger, "alpha", {"measure": {"command": "echo 7"}}, self.folder, now=NOW)
        self.assertEqual((got["exit_code"], got["line"]), (0, "7"))

    def test_digest_last_five_newest_first_and_unchanged_without(self):
        p = self.reg.projects["alpha"]
        before = handshake.build_digest(self.reg, p, self.ledger, now=NOW)
        for i in range(7):
            self.ledger.record_reading("alpha", "followers", 0, f"n{i}", NOW - 100 + i)
        text = handshake.build_digest(self.reg, p, self.ledger, now=NOW)
        self.assertTrue(text.startswith(before))
        section = text[len(before):]
        self.assertIn("## Measure", section)
        self.assertEqual([section.index(f"n{i}") for i in (6, 5, 4, 3, 2)], sorted(section.index(f"n{i}") for i in (6, 5, 4, 3, 2)))
        self.assertNotIn("n1", section)
        self.assertNotIn("Measure", before)

    def test_digest_shows_failed_reading(self):
        self.ledger.record_reading("alpha", "followers", 1, "boom", NOW)
        text = handshake.build_digest(self.reg, self.reg.projects["alpha"], self.ledger, now=NOW)
        self.assertIn("FAILED (exit 1) boom", text)


if __name__ == "__main__":
    unittest.main()
