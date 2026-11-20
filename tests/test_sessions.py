"""The owner's own Claude Code sessions as spans per project, and their place in the pie (commonroom#239). Every
transcript here is made up, in a temporary folder: never ~/.claude/projects."""
import builtins
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from commonroom import cli, sessions, standings, usage
from commonroom.registry import Registry
from tests import test_standings
from tests.test_standings import H

SECRET = "the owner's private words"
T0 = 1_790_000_000.0          # in the past, so files written now are not too old to open


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def assistant(at: float, session: str, cwd: str, message_id: str | None = None, tokens: int = 100) -> dict:
    message = {"role": "assistant", "model": "claude-test", "content": [{"type": "text", "text": SECRET}],
               "usage": {"input_tokens": tokens, "output_tokens": tokens, "cache_read_input_tokens": 0}}
    if message_id is not None:
        message["id"] = message_id
    return {"type": "assistant", "timestamp": stamp(at), "sessionId": session, "cwd": cwd, "message": message}


def user(at: float, session: str, cwd: str) -> dict:
    return {"type": "user", "timestamp": stamp(at), "sessionId": session, "cwd": cwd,
            "message": {"role": "user", "content": SECRET + ' "type":"assistant"'}}


class Transcripts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "claude-projects"
        self.dir.mkdir()
        self.alpha = self.root / "work" / "alpha"
        self.beta = self.root / "work" / "beta"
        for folder in (self.alpha, self.beta):
            folder.mkdir(parents=True)
        self.projects = {"alpha": str(self.alpha), "beta": str(self.beta)}

    def write(self, started_in, session: str, lines: list, subagent: str | None = None, raw: list = ()) -> Path:
        """A transcript in the folder Claude Code would use for a session started in `started_in`."""
        folder = self.dir / usage.session_folder(started_in)
        path = folder / session / "subagents" / f"agent-{subagent}.jsonl" if subagent else folder / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
            for line in raw:
                handle.write(line + "\n")
        return path

    def spans(self, start=T0 - 86400, end=T0 + 86400, **kwargs):
        return sessions.spans(self.projects, start, end, projects_dir=self.dir, **kwargs)

    def test_messages_two_minutes_apart_are_one_span(self):
        cwd = str(self.alpha)
        self.write(cwd, "s1", [assistant(T0 + i * 120, "s1", cwd, f"m{i}") for i in range(5)])
        found = self.spans()
        self.assertEqual(len(found), 1)
        span = found[0]
        self.assertEqual((span["project"], span["session_id"], span["messages"]), ("alpha", "s1", 5))
        self.assertAlmostEqual(span["started_at"], T0 - sessions.LEAD, places=2)
        self.assertAlmostEqual(span["ended_at"], T0 + 480, places=2)
        self.assertEqual(span["tokens"], 5 * 200)

    def test_messages_three_hours_apart_are_two_spans(self):
        cwd = str(self.alpha)
        self.write(cwd, "s1", [assistant(T0, "s1", cwd, "m1"), assistant(T0 + 3 * H, "s1", cwd, "m2")])
        found = self.spans()
        self.assertEqual([(s["project"], s["messages"]) for s in found], [("alpha", 1), ("alpha", 1)])
        self.assertAlmostEqual(found[1]["started_at"], T0 + 3 * H - sessions.LEAD, places=2)

    def test_a_call_written_as_several_lines_counts_once(self):
        cwd = str(self.alpha)
        self.write(cwd, "s1", [assistant(T0, "s1", cwd, "m1", 10), assistant(T0 + 1, "s1", cwd, "m1", 50)])
        [span] = self.spans()
        self.assertEqual((span["messages"], span["tokens"]), (1, 100))

    def test_a_session_in_the_folder_or_a_worktree_under_it_goes_to_that_project(self):
        inside = self.alpha / ".claude" / "worktrees" / "agent-1"
        inside.mkdir(parents=True)
        self.write(self.alpha, "s1", [assistant(T0, "s1", str(self.alpha), "m1")])
        self.write(inside, "s2", [assistant(T0, "s2", str(inside), "m1")])
        self.write(self.beta, "s3", [assistant(T0, "s3", str(self.beta), "m1")])
        self.assertEqual({(s["project"], s["session_id"]) for s in self.spans()},
                         {("alpha", "s1"), ("alpha", "s2"), ("beta", "s3")})

    def test_a_session_elsewhere_goes_to_nobody_even_when_its_folder_name_looks_alike(self):
        sibling = Path(str(self.alpha) + "-wt") / "room" / "i1"         # encodes with alpha's folder name as a prefix
        sibling.mkdir(parents=True)
        dotted = Path(str(self.alpha) + ".old")                          # `.` encodes as `-`, like `/`
        dotted.mkdir()
        other = self.root / "elsewhere"
        other.mkdir()
        self.write(sibling, "s1", [assistant(T0, "s1", str(sibling), "m1")])
        self.write(dotted, "s2", [assistant(T0, "s2", str(dotted), "m1")])
        self.write(other, "s3", [assistant(T0, "s3", str(other), "m1")])
        self.assertEqual(self.spans(), [])

    def test_a_subagent_goes_by_its_own_cwd_and_joins_its_sessions_spans(self):
        cwd, tree = str(self.alpha), self.alpha / ".claude" / "worktrees" / "b"
        tree.mkdir(parents=True)
        self.write(cwd, "s1", [assistant(T0, "s1", cwd, "m1")])
        self.write(cwd, "s1", [assistant(T0 + 120, "s1", str(tree), "m2"), assistant(T0 + 240, "s1", str(tree), "m3")],
                   subagent="a1")
        self.write(cwd, "s1", [assistant(T0 + 300, "s1", str(self.beta), "m4")], subagent="a2")   # it works in beta
        found = {(s["project"], s["session_id"]): s for s in self.spans()}
        self.assertEqual(set(found), {("alpha", "s1"), ("beta", "s1")})
        self.assertEqual(found["alpha", "s1"]["messages"], 3)
        self.assertAlmostEqual(found["alpha", "s1"]["ended_at"], T0 + 240, places=2)

    def test_a_room_workers_folder_and_session_are_left_out(self):
        worker = self.alpha / "wt" / "room-i5"
        worker.mkdir(parents=True)
        cwd = str(self.alpha)
        self.write(worker, "w1", [assistant(T0, "w1", str(worker), "m1")])
        self.write(cwd, "w2", [assistant(T0, "w2", cwd, "m1")])
        self.write(cwd, "s1", [assistant(T0, "s1", cwd, "m1")])
        self.assertEqual({s["session_id"] for s in self.spans()}, {"w1", "w2", "s1"})
        # The ledger stores worker folders case-folded, as `Ledger.set_worktree` does.
        left = self.spans(exclude_dirs=[os.path.normcase(os.path.abspath(worker))], exclude_sessions=["w2"])
        self.assertEqual({s["session_id"] for s in left}, {"s1"})

    def test_a_malformed_line_is_skipped(self):
        cwd = str(self.alpha)
        good = assistant(T0, "s1", cwd, "m1")
        bad_time = dict(assistant(T0 + 60, "s1", cwd, "m2"), timestamp="yesterday")
        no_usage = assistant(T0 + 70, "s1", cwd, "m3")
        del no_usage["message"]["usage"]
        no_cwd = assistant(T0 + 80, "s1", cwd, "m4")
        del no_cwd["cwd"]
        self.write(cwd, "s1", [bad_time, no_usage, no_cwd, {"type": "assistant", "message": None}],
                   raw=['{"type":"assistant", "timestamp": ', "\x00not json", json.dumps(good)])
        [span] = self.spans()
        self.assertEqual(span["messages"], 1)
        self.assertAlmostEqual(span["ended_at"], T0, places=2)

    def test_nothing_returned_holds_message_text(self):
        cwd = str(self.alpha)
        self.write(cwd, "s1", [user(T0 - 30, "s1", cwd), assistant(T0, "s1", cwd, "m1"), user(T0 + 30, "s1", cwd)])
        found = self.spans()
        self.assertEqual(len(found), 1)
        self.assertEqual(set(found[0]), {"project", "session_id", "started_at", "ended_at", "messages", "tokens"})
        text = json.dumps(found)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("claude-test", text)

    def test_only_messages_inside_the_span_count_and_old_files_are_not_opened(self):
        cwd = str(self.alpha)
        self.write(cwd, "s1", [assistant(T0 - 2 * H, "s1", cwd, "m0"), assistant(T0, "s1", cwd, "m1")])
        old = self.write(cwd, "s2", [assistant(T0 - 5 * H, "s2", cwd, "m1")])
        os.utime(old, (T0 - 4 * H, T0 - 4 * H))
        opened = []
        real_open = builtins.open
        with mock.patch.object(builtins, "open", side_effect=lambda p, *a, **k: opened.append(Path(p)) or real_open(p, *a, **k)):
            found = self.spans(start=T0 - H, end=T0 + H)
        self.assertEqual([(s["session_id"], s["messages"]) for s in found], [("s1", 1)])
        self.assertNotIn(old, opened)

    def test_with_no_project_nothing_is_read(self):
        cwd = str(self.alpha)
        self.write(cwd, "s1", [assistant(T0, "s1", cwd, "m1")])
        with mock.patch.object(builtins, "open", side_effect=AssertionError("read a transcript")), \
                mock.patch.object(Path, "iterdir", side_effect=AssertionError("listed transcripts")):
            self.assertEqual(sessions.spans({}, T0 - H, T0 + H, projects_dir=self.dir), [])

    @unittest.skipUnless(os.name == "nt", "paths are case-insensitive on Windows only")
    def test_paths_compare_case_insensitively_on_windows(self):
        cwd = str(self.alpha).upper()
        self.write(str(self.alpha).lower(), "s1", [assistant(T0, "s1", cwd, "m1")])
        self.assertEqual([s["project"] for s in self.spans()], ["alpha"])


class OwnSessionsInTheStandings(test_standings.Room):
    """`Room.week()`'s meter, 10 -> 16 -> 20, with no room run: alpha's owner works in alpha's folder through the
    10 -> 16 gap, so those 6 points are alpha's own sessions; the 4 after are still the owner's unassigned use.
    `now` is a whole second, so transcript timestamps (to the millisecond) keep the hand-worked figures exact."""

    def setUp(self):
        super().setUp()
        self.now = float(int(time.time()))
        self.resets = self.now + 3.5 * 86400

    def session(self, folder: str, session: str, start: float, end: float, step: float = 120):
        """Assistant messages every `step` seconds from `start`, and one at `end`."""
        path = self.transcripts / usage.session_folder(folder) / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        times = [start + i * step for i in range(int((end - start) // step) + 1)] + [end]
        path.write_text("".join(json.dumps(assistant(at, session, folder, f"{session}-{i}")) + "\n"
                                for i, at in enumerate(times)), encoding="utf-8")

    def readings(self):
        for at, pct in ((self.now - 3 * H, 10), (self.now - 2 * H, 16), (self.now - H, 20)):
            self.reading(at, pct)

    def test_a_meter_step_during_an_own_session_is_that_projects_not_the_owners(self):
        self.readings()
        reg = self.register()
        before = standings.standings(self.ledger, reg, "claude", self.now)
        self.assertEqual(self.spent(before), {"alpha": 0, "beta": 0})
        self.assertAlmostEqual(before.owner_use, 20.0)
        self.session(reg.projects["alpha"].path, "s1", self.now - 2.9 * H, self.now - 2.1 * H)
        report = standings.standings(self.ledger, reg, "claude", self.now)
        self.assertEqual(self.spent(report), {"alpha": 6.0, "beta": 0})
        self.assertAlmostEqual(report.own["alpha"], 6.0)
        self.assertAlmostEqual(report.room("alpha"), 0.0)
        self.assertAlmostEqual(report.owner_use, 14.0)
        self.assertEqual(report.unregistered, {})

    def test_own_sessions_and_room_runs_share_a_gap_by_time(self):
        self.readings()
        reg = self.register()
        self.add_run("beta-site", self.now - 2.5 * H, self.now - 2 * H, cost=1)        # half an hour of the gap
        self.session(reg.projects["alpha"].path, "s1", self.now - 2.5 * H + sessions.LEAD, self.now - 2 * H)
        report = standings.standings(self.ledger, reg, "claude", self.now)
        self.assertEqual(self.spent(report), {"alpha": 3.0, "beta": 3.0})
        self.assertAlmostEqual(report.own["alpha"], 3.0)
        self.assertAlmostEqual(report.room("beta"), 3.0)
        self.assertAlmostEqual(report.own["beta"], 0.0)

    def test_a_room_workers_own_transcript_is_not_counted_twice(self):
        self.readings()
        reg = self.register()
        run = self.ledger.start_run("alpha-site", "#1", "builder", "claude", model="sonnet", session_id="w1",
                                    now=self.now - 3 * H)
        self.ledger.finish_run(run, "done", cost_usd=1, now=self.now - 2 * H)
        self.session(reg.projects["alpha"].path, "w1", self.now - 2.9 * H, self.now - 2.1 * H)
        report = standings.standings(self.ledger, reg, "claude", self.now)
        self.assertEqual(self.spent(report), {"alpha": 6.0, "beta": 0})
        self.assertAlmostEqual(report.own["alpha"], 0.0)

    def test_codex_reads_no_transcripts(self):
        self.reading(self.now - 2 * H, 10, provider="codex")
        self.reading(self.now - H, 20, provider="codex")
        reg = self.register()
        self.session(reg.projects["alpha"].path, "s1", self.now - 1.9 * H, self.now - 1.1 * H)
        with mock.patch.object(sessions, "spans", side_effect=AssertionError("read transcripts for codex")):
            report = standings.standings(self.ledger, reg, "codex", self.now)
        self.assertEqual(self.spent(report), {"alpha": 0, "beta": 0})

    def test_an_empty_registry_reads_no_transcript(self):
        self.readings()
        self.session(str(self.root / "work" / "alpha"), "s1", self.now - 2.9 * H, self.now - 2.1 * H)
        with mock.patch.object(builtins, "open", side_effect=AssertionError("read a transcript")):
            report = standings.standings(self.ledger, Registry(), "claude", self.now)
        self.assertEqual(report.rows, [])
        self.assertAlmostEqual(report.owner_use, 20.0)


    def test_room_pie_shows_room_and_own_sessions(self):
        self.readings()
        reg = self.register()
        self.add_run("beta-site", self.now - 2.5 * H, self.now - 2 * H, cost=1)
        self.session(reg.projects["alpha"].path, "s1", self.now - 2.5 * H + sessions.LEAD, self.now - 2 * H)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["pie"]), 0)
        text = out.getvalue()
        self.assertRegex(text, r"spent est\.\s+room\s+own sessions")
        self.assertRegex(text, r"alpha\s+40\s+paced\s+3\.0\s+0\.0\s+3\.0\s")
        self.assertRegex(text, r"beta\s+20\s+spare\s+3\.0\s+3\.0\s+0\.0\s")
        self.assertIn("projects est. 6.0%; owner's slice 40, his own use est. 14.0%", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["pie", "--json"]), 0)
        rows = {p["name"]: p for p in json.loads(out.getvalue())["providers"]["claude"]["projects"]}
        self.assertAlmostEqual(rows["alpha"]["own_sessions_est"], 3.0)
        self.assertAlmostEqual(rows["alpha"]["room_est"], 0.0)
        self.assertAlmostEqual(rows["beta"]["room_est"], 3.0)


if __name__ == "__main__":
    unittest.main()
