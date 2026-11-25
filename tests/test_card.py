"""A place's card (commonroom#572): one screen, every line with its source, missing shown as missing."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import card, cli, ledger as ledger_mod, places, registry


def msg(cwd, sid, ts):
    return json.dumps({"type": "assistant", "sessionId": sid, "cwd": str(cwd), "timestamp": ts,
                       "message": {"id": "m" + sid + ts, "usage": {"input_tokens": 1}}}) + "\n"


class CardTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.records, self.work = self.root / "home", self.root / "claude" / "projects", self.root / "work"
        for d in (self.home, self.records, self.work):
            d.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home), "HOME": str(self.root),
                                           "USERPROFILE": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        patch = mock.patch("tempfile.gettempdir", return_value=str(self.root / "tmpdir"))
        patch.start()
        self.addCleanup(patch.stop)
        self.proj = self.work / "proj"
        (self.proj / "ways").mkdir(parents=True)
        (self.proj / "room.toml").write_text('[project]\nname="proj"\nmission="Make widgets."\n[roles]\ndirector=["claude:opus"]\n')
        self.reg = registry.Registry(projects={"proj": registry.Project("proj", str(self.proj), 50)})
        self.ledger = ledger_mod.Ledger(self.home / "room.sqlite3")
        self.addCleanup(self.ledger.db.close)

    def session(self, folder, sid, ts, title=None, n=1):
        d = self.records / "enc"
        d.mkdir(exist_ok=True)
        with open(d / f"{sid}.jsonl", "a", encoding="utf-8", newline="") as f:
            for i in range(n):
                f.write(msg(folder, sid, ts.replace("00:00", f"0{i}:00")))
            if title:
                f.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}) + "\n")

    def test_project_card(self):
        run = self.ledger.start_run("proj", "issue-1", "builder", "claude")
        self.ledger.finish_run(run, "done", outputs=[{"kind": "merge", "ref": "PR #1"}])
        self.ledger.trip("proj", "no_output", "three empty runs")
        self.ledger.record_heartbeat("proj", "refresh", 1)
        lines = card.build("proj", self.reg, places.load(), self.ledger, self.records)
        text = "\n".join(lines)
        self.assertIn("Make widgets. [room.toml mission]", text)
        self.assertIn("last landing", text)
        self.assertIn("no_output: three empty runs", text)
        self.assertIn("failing heartbeat commands: refresh", text)
        self.assertIn("ways/ (folder)", text)
        self.assertIn("result:", text)
        self.assertIn("its director (claude:opus) [room.toml roles]", text)
        self.assertIn("room hand", lines[-1])
        self.assertTrue(all("[" in l for l in lines[:-1]), lines)
        (self.proj / "sub").mkdir()
        self.assertEqual(card.build(str(self.proj / "sub"), self.reg, places.load(), self.ledger, self.records)[0], lines[0])

    def test_plain_place_card_and_missing_shown(self):
        folder = self.work / "garden"
        folder.mkdir()
        self.session(folder, "s1", "2026-11-01T00:00:00+00:00", title="Plan the beds", n=3)
        self.session(folder, "s2", "2026-11-02T00:00:00+00:00")
        data = places.discover(self.reg, self.records)
        lines = card.build("garden", self.reg, data, self.ledger, self.records)
        text = "\n".join(lines)
        self.assertIn("a place (no room.toml, no share)", text)
        self.assertIn("for: missing", text)
        self.assertIn("method: missing", text)
        self.assertIn("results: missing", text)
        self.assertIn("Plan the beds (s1, 3 messages)", text)
        self.assertIn("(untitled) (s2, 1 messages)", text)
        self.assertLess(text.index("s1"), text.index("s2"))
        places.describe(data, "garden", "Beds and seeds")
        self.assertIn("Beds and seeds [places.json]", "\n".join(card.build("garden", self.reg, data, self.ledger, self.records)))
        folder.rmdir()
        self.assertIn("GONE from disk", "\n".join(card.build("garden", self.reg, data, self.ledger, self.records)))

    def test_unknown_refused_and_cli_writes_nothing(self):
        with self.assertRaises(card.CardError):
            card.build("nowhere", self.reg, places.load(), self.ledger, self.records)
        before = sorted(p.name for p in self.home.iterdir())
        err = io.StringIO()
        with mock.patch("commonroom.registry.load", return_value=self.reg), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["card", "nowhere"]), 1)
        self.assertIn("refused", err.getvalue())
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
