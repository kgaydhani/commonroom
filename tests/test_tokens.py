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
        self.codex = self.root / "codex"
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home), "CODEX_HOME": str(self.codex)})
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


def codex_lines(session, events, model="gpt-x", text=SECRET):
    """events: (at, input, cached, output) running totals."""
    out = [{"type": "session_meta", "timestamp": "2020-01-01T00:00:00Z", "payload": {"id": session, "cwd": "D:\\work\\demo"}},
           {"type": "turn_context", "timestamp": "2020-01-01T00:00:01Z", "payload": {"model": model}},
           {"type": "response_item", "payload": {"type": "message", "content": text}}]
    for at, i, c, o in events:
        use = {"input_tokens": i, "cached_input_tokens": c, "cache_write_input_tokens": 0, "output_tokens": o,
               "reasoning_output_tokens": 1, "total_tokens": i + o}
        out.append({"type": "event_msg", "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                    "payload": {"type": "token_count", "info": {"total_token_usage": use, "last_token_usage": use}}})
    return "".join(json.dumps(x) + "\n" for x in out)


class CodexRows(Collect):
    def put(self, name, text):
        path = self.codex / "sessions" / "2026" / "11" / "21" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_cached_split_and_cumulative_per_day(self):
        self.put("rollout-a.jsonl", codex_lines("c1", [(T0 + 10, 100, 60, 10), (T0 + 20, 100, 60, 10), (T0 + 30, 250, 150, 30),
                                                       (T0 + DAY + 5, 400, 300, 50)]))
        self.assertIn("added 2 rows", self.run_cli())
        rows = self.rows()
        self.assertEqual([(r["day"], r["session"], r["model"], r["folder"], r["input"], r["output"], r["cache_write"], r["cache_read"])
                          for r in rows],
                         [(rows[0]["day"], "c1", "gpt-x", "demo", 100, 30, 0, 150), (rows[1]["day"], "c1", "gpt-x", "demo", 0, 20, 0, 150)])
        self.assertNotEqual(rows[0]["day"], rows[1]["day"])

    def test_no_text_rerun_and_growth(self):
        path = self.put("rollout-a.jsonl", codex_lines("c1", [(T0 + 10, 100, 60, 10)]))
        self.run_cli()
        self.assertIn("added 0 rows", self.run_cli())
        path.write_text(codex_lines("c1", [(T0 + 10, 100, 60, 10), (T0 + 20, 130, 70, 15)]), encoding="utf-8")
        self.assertIn("added 1 rows", self.run_cli())
        self.assertEqual([r["input"] for r in self.rows()], [40, 20])
        for f in self.home.rglob("*"):
            if f.is_file():
                self.assertNotIn(SECRET, f.read_text(encoding="utf-8", errors="replace"))

    def test_missing_codex_folder_is_fine_and_bad_line_counted(self):
        self.assertIn("added 0 rows", self.run_cli())
        self.put("rollout-b.jsonl", "not json\n" + codex_lines("c2", [(T0 + 10, 5, 1, 2)]))
        out = self.run_cli()
        self.assertIn("added 1 rows", out)
        self.assertIn("skipped 1 malformed", out)
