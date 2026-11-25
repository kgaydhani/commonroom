"""`room tokens collect` (commonroom#641). Every transcript here is made up, in a temporary folder."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from commonroom import cli, sessions, tokens

SECRET = "distinctive-secret-words-7731"
DAY = 86400.0
T0 = 1_760_000_000.0 - (1_760_000_000.0 % DAY)       # a UTC midnight, long ago


def line(at, session, model, mid, inp=10, out=20, cw=30, cr=40, text=SECRET):
    return json.dumps({"type": "assistant", "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(), "sessionId": session,
                       "cwd": "/x", "message": {"id": mid, "model": model, "content": [{"type": "text", "text": text}],
                       "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
                                 "cache_read_input_tokens": cr}}}) + "\n"


class Collect(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.projects = self.root / "claude" / "projects"
        self.home = self.root / "home"
        self.folder = self.projects / "D--proj-one"
        (self.folder / "s1" / "subagents").mkdir(parents=True)
        self.main = self.folder / "s1.jsonl"
        self.sub = self.folder / "s1" / "subagents" / "agent-a.jsonl"
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self):
        buf = io.StringIO()
        with mock.patch.object(sessions, "default_dir", return_value=self.projects), contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["tokens", "collect"]), 0)
        return buf.getvalue()

    def rows(self):
        path = self.home / "token-rows.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def write_sample(self):
        self.main.write_text(line(T0 + 10, "s1", "m-big", "a") + line(T0 + 20, "s1", "m-small", "b")
                             + line(T0 + DAY + 5, "s1", "m-big", "c") + line(T0 + 2 * DAY + 5, "s1", "m-big", "d"),
                             encoding="utf-8")
        self.sub.write_text(line(T0 + 30, "s1", "m-small", "e"), encoding="utf-8")

    def test_rows_per_session_model_day(self):
        self.write_sample()
        self.assertIn("added 4 rows", self.run_cli())
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        small = next(r for r in rows if r["model"] == "m-small")
        self.assertEqual((small["input"], small["output"], small["cache_write"], small["cache_read"]), (20, 40, 60, 80))
        self.assertEqual((small["session"], small["folder"], small["first"], small["last"]),
                         ("s1", "D--proj-one", tokens._iso(T0 + 20), tokens._iso(T0 + 30)))
        self.assertEqual(sorted({r["day"] for r in rows}), [tokens._iso(T0 + n * DAY)[:10] for n in range(3)])

    def test_no_text_stored(self):
        self.write_sample()
        self.run_cli()
        self.assertNotIn(SECRET, (self.home / "token-rows.jsonl").read_text(encoding="utf-8"))
        self.assertFalse(any(SECRET in p.read_text(encoding="utf-8") for p in self.home.iterdir() if p.is_file()))

    def test_twice_adds_nothing_and_growth_adds_only_new(self):
        self.write_sample()
        self.run_cli()
        self.assertIn("added 0 rows", self.run_cli())
        self.assertEqual(len(self.rows()), 4)
        with open(self.main, "a", encoding="utf-8") as f:
            f.write(line(T0 + 50, "s1", "m-big", "f", inp=1, out=2, cw=3, cr=4))
            f.write(line(T0 + 50, "s1", "m-big", "f", inp=1, out=2, cw=3, cr=4))      # same message again: one call
        self.assertIn("added 1 rows", self.run_cli())
        new = self.rows()[-1]
        self.assertEqual((new["model"], new["input"], new["output"], new["cache_write"], new["cache_read"]), ("m-big", 1, 2, 3, 4))
        self.assertEqual(len(self.rows()), 5)

    def test_repeated_message_id_counts_once_with_largest(self):
        self.main.write_text(line(T0, "s1", "m", "a", out=5) + line(T0 + 1, "s1", "m", "a", out=50), encoding="utf-8")
        self.run_cli()
        self.assertEqual([(r["input"], r["output"]) for r in self.rows()], [(10, 50)])

    def test_backfill_whatever_the_age(self):
        old = 1_000_000_000.0
        self.main.write_text(line(old, "s1", "m", "a"), encoding="utf-8")
        os.utime(self.main, (old, old))
        self.run_cli()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["day"], "2001-09-09")

    def test_malformed_and_unreadable_are_skipped_and_counted(self):
        self.main.write_text(line(T0, "s1", "m", "a") + '{"type":"assistant", broken\n' + line(T0 + 1, "s1", "m", "b"),
                             encoding="utf-8")
        real_open = open

        def fake_open(path, *a, **k):
            if str(path).endswith("agent-a.jsonl"):
                raise PermissionError("locked")
            return real_open(path, *a, **k)
        self.sub.write_text(line(T0, "s1", "m", "z"), encoding="utf-8")
        with mock.patch("builtins.open", fake_open):
            out = self.run_cli()
        self.assertIn("added 1 rows", out)
        self.assertIn("1 malformed lines, 1 unreadable files", out)
        self.assertEqual(self.rows()[0]["input"], 20)

    def test_sessions_cache_still_answers_spans(self):
        self.write_sample()
        self.run_cli()
        found = sessions.spans({"p": str(Path("/x"))}, 0.0, T0 + 10 * DAY, projects_dir=self.projects,
                               cache=self.home / "c.json")
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
