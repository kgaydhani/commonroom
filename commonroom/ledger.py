"""Durable record of usage readings, runs, their outputs and breaker trips."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    observed_at REAL NOT NULL,
    first_seen REAL NOT NULL,
    provider TEXT NOT NULL,
    window TEXT NOT NULL,
    used_pct REAL NOT NULL,
    resets_at REAL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_latest ON usage (provider, window, observed_at);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project TEXT,
    task TEXT,
    role TEXT,
    provider TEXT,
    model TEXT,
    effort TEXT,
    autonomous INTEGER NOT NULL DEFAULT 1,
    started_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL,
    session_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    outputs TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    weekly_start REAL,
    weekly_end REAL,
    five_hour_start REAL
);
CREATE INDEX IF NOT EXISTS runs_recent ON runs (project, started_at);
CREATE TABLE IF NOT EXISTS breakers (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    project TEXT,
    kind TEXT NOT NULL,
    detail TEXT,
    cleared_at REAL
);
CREATE TABLE IF NOT EXISTS cooldowns (
    provider TEXT PRIMARY KEY,
    until REAL NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS marks (
    project TEXT NOT NULL,
    key TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (project, key)
);
"""


def _path_key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def default_path() -> Path:
    home = Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom")
    return home / "room.sqlite3"


_SYNCHRONOUS = {"NORMAL": 1, "OFF": 0}        # FULL costs an fsync per commit; WAL is safe with NORMAL


class Ledger:
    def __init__(self, path: Path | str | None = None, synchronous: str = "NORMAL"):
        pragma = _SYNCHRONOUS[synchronous]
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA synchronous={pragma}")
        self.db.executescript(SCHEMA)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        for name, kind in (("weekly_start", "REAL"), ("weekly_end", "REAL"), ("pid", "INTEGER"), ("worktree", "TEXT"),
                           ("five_hour_start", "REAL"), ("unverified", "TEXT NOT NULL DEFAULT '[]'"),
                           ("actions_checked", "INTEGER")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")

    def close(self):
        self.db.close()

    # Usage -----------------------------------------------------------------
    def record_usage(self, observations: list[dict], now: float | None = None) -> int:
        """Insert changed readings; refresh `observed_at` on unchanged ones so freshness stays true."""
        now = time.time() if now is None else now
        changed = 0
        with self.db:
            for o in observations:
                last = self.db.execute(
                    "SELECT id, used_pct, resets_at FROM usage WHERE provider=? AND window=? "
                    "ORDER BY observed_at DESC LIMIT 1", (o["provider"], o["window"])).fetchone()
                same = (last is not None and abs(last["used_pct"] - o["used_pct"]) < 1e-6
                        and abs((last["resets_at"] or 0) - (o.get("resets_at") or 0)) < 90)
                if same:
                    self.db.execute("UPDATE usage SET observed_at=?, source=? WHERE id=?", (now, o["source"], last["id"]))
                else:
                    self.db.execute(
                        "INSERT INTO usage (observed_at, first_seen, provider, window, used_pct, resets_at, source) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (now, now, o["provider"], o["window"], o["used_pct"], o.get("resets_at"), o["source"]))
                    changed += 1
        return changed

    def latest_usage(self, provider: str) -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT window, used_pct, resets_at, observed_at, source FROM usage u WHERE provider=? AND observed_at = "
            "(SELECT MAX(observed_at) FROM usage WHERE provider=u.provider AND window=u.window)", (provider,)).fetchall()
        return {r["window"]: dict(r) for r in rows}

    def usage_history(self, provider: str, window: str, since: float) -> list[dict]:
        rows = self.db.execute(
            "SELECT first_seen, observed_at, used_pct, resets_at FROM usage WHERE provider=? AND window=? "
            "AND observed_at>=? ORDER BY first_seen", (provider, window, since)).fetchall()
        return [dict(r) for r in rows]

    # Runs ------------------------------------------------------------------
    def start_run(self, project: str | None, task: str | None, role: str, provider: str, model: str | None = None,
                  effort: str | None = None, autonomous: bool = True, session_id: str | None = None,
                  now: float | None = None, exclusive: bool = False) -> str | None:
        """With `exclusive`, claim the task atomically across dispatchers: None if it is already running."""
        run_id = uuid.uuid4().hex[:12]
        now_ts = time.time() if now is None else now
        with self.db:
            if exclusive:
                if self.db.in_transaction:
                    self.db.commit()
                self.db.execute("BEGIN IMMEDIATE")  # write lock first: two dispatchers can't both pass the check
                # A worker never runs past an hour; older 'running' rows are crashed dispatchers, not claims.
                if self.db.execute("SELECT 1 FROM runs WHERE project IS ? AND task=? AND status='running' AND started_at>?",
                                   (project, task, now_ts - 7200)).fetchone():
                    return None
            self.db.execute(
                "INSERT INTO runs (id, project, task, role, provider, model, effort, autonomous, started_at, status, session_id, pid) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, project, task, role, provider, model, effort, int(autonomous), now_ts, "running", session_id, os.getpid()))
        return run_id

    def running_tasks(self, project: str | None, now: float | None = None) -> set[str]:
        now = time.time() if now is None else now
        rows = self.db.execute("SELECT task FROM runs WHERE project IS ? AND status='running' AND started_at>? AND task IS NOT NULL",
                               (project, now - 7200)).fetchall()
        return {r["task"] for r in rows}

    def finish_run(self, run_id: str, status: str, outputs: list[dict] | None = None, input_tokens: int | None = None,
                   output_tokens: int | None = None, cost_usd: float | None = None, note: str | None = None,
                   session_id: str | None = None, now: float | None = None):
        with self.db:
            self.db.execute(
                "UPDATE runs SET ended_at=?, status=?, outputs=?, input_tokens=?, output_tokens=?, cost_usd=?, note=?, "
                "session_id=COALESCE(?, session_id) WHERE id=?",
                (time.time() if now is None else now, status, json.dumps(outputs or []), input_tokens, output_tokens,
                 cost_usd, note, session_id, run_id))

    def recent_runs(self, project: str | None = None, limit: int = 20, autonomous: bool | None = None,
                    finished: bool = False, provider: str | None = None) -> list[dict]:
        clauses, params = [], []
        if project is not None:
            clauses.append("project=?"); params.append(project)
        if provider is not None:
            clauses.append("provider=?"); params.append(provider)
        if autonomous is not None:
            clauses.append("autonomous=?"); params.append(int(autonomous))
        if finished:
            # Room-side errors, provider usage limits and gate stops say nothing about the work, so breakers ignore them.
            clauses.append("status NOT IN ('running', 'room-error', 'limited', 'stopped')")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.execute(f"SELECT * FROM runs {where} ORDER BY started_at DESC LIMIT ?", (*params, limit)).fetchall()
        result = []
        for r in rows:
            row = dict(r)
            row["outputs"] = json.loads(row["outputs"] or "[]")
            row["unverified"] = json.loads(row["unverified"] or "[]")
            result.append(row)
        return result

    def task_runs(self, project: str | None, task: str, limit: int = 3, exclude: str | None = None) -> list[dict]:
        """The task's latest runs in this project, newest first (`exclude` leaves out the run being started)."""
        rows = self.db.execute("SELECT * FROM runs WHERE project IS ? AND task=? AND id IS NOT ? ORDER BY started_at DESC LIMIT ?",
                               (project, task, exclude, limit)).fetchall()
        return [dict(r) for r in rows]

    def record_claims(self, run_id: str, checked: int, unverified: list[str]):
        """How many of a run's ACTIONS claims were checked against GitHub, and the ones that didn't check out."""
        with self.db:
            self.db.execute("UPDATE runs SET actions_checked=?, unverified=? WHERE id=?",
                            (checked, json.dumps(unverified), run_id))

    def projects(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT DISTINCT project FROM runs WHERE project IS NOT NULL")}

    def worker_dirs(self, since: float) -> list[str]:
        """Folders where room workers of any project ran at some point since `since` (still running or ended later)."""
        rows = self.db.execute("SELECT DISTINCT worktree FROM runs WHERE worktree IS NOT NULL AND (status='running' OR ended_at>=?)",
                               (since,)).fetchall()
        return [r["worktree"] for r in rows]

    def running_runs(self, project: str | None) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs WHERE project IS ? AND status='running'", (project,))]

    def close_orphan(self, run_id: str, note: str, now: float | None = None) -> bool:
        """Close a run whose dispatcher died. True only for the one caller that closed it."""
        with self.db:
            return self.db.execute("UPDATE runs SET status='stopped', ended_at=?, note=? WHERE id=? AND status='running'",
                                   (time.time() if now is None else now, note, run_id)).rowcount == 1

    def set_worktree(self, run_id: str, path) -> None:
        with self.db:
            self.db.execute("UPDATE runs SET worktree=? WHERE id=?", (_path_key(path), run_id))

    def worktree_in_use(self, path, exclude_run: str | None = None, now: float | None = None) -> dict | None:
        """The running run (if any) whose working copy is `path`: nobody may remove it from under a live worker."""
        now = time.time() if now is None else now
        row = self.db.execute("SELECT * FROM runs WHERE worktree=? AND status='running' AND started_at>? AND id IS NOT ?",
                              (_path_key(path), now - 7200, exclude_run)).fetchone()
        return dict(row) if row else None

    def room_error_streaks(self, project: str | None, now: float | None = None) -> dict[str, tuple[int, float]]:
        """Per task, how many of its latest runs in a row were room-errors, and when the last one ended."""
        now = time.time() if now is None else now
        streaks: dict[str, tuple[int, float]] = {}
        settled = set()
        for r in self.db.execute("SELECT task, status, ended_at FROM runs WHERE project IS ? AND task IS NOT NULL "
                                 "AND status != 'running' AND started_at > ? ORDER BY started_at DESC",
                                 (project, now - 86400)):
            if r["task"] in settled:
                continue
            if r["status"] != "room-error":
                settled.add(r["task"])
                continue
            count, last = streaks.get(r["task"], (0, r["ended_at"] or 0))
            streaks[r["task"]] = (count + 1, last)
        return streaks

    def task_failures(self, project: str | None, task: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM runs WHERE project IS ? AND task=? AND status IN ('failed', 'blocked')",
                               (project, task)).fetchone()[0]

    # Breakers --------------------------------------------------------------
    def trip(self, project: str | None, kind: str, detail: str, now: float | None = None):
        if any(b["kind"] == kind for b in self.open_breakers(project)):
            return
        with self.db:
            self.db.execute("INSERT INTO breakers (at, project, kind, detail) VALUES (?,?,?,?)",
                            (time.time() if now is None else now, project, kind, detail))

    def open_breakers(self, project: str | None) -> list[dict]:
        rows = self.db.execute("SELECT * FROM breakers WHERE project IS ? AND cleared_at IS NULL ORDER BY at",
                               (project,)).fetchall()
        return [dict(r) for r in rows]

    def clear_breakers(self, project: str | None, kind: str | None = None, now: float | None = None) -> int:
        query, params = "UPDATE breakers SET cleared_at=? WHERE project IS ? AND cleared_at IS NULL", [time.time() if now is None else now, project]
        if kind:
            query += " AND kind=?"; params.append(kind)
        with self.db:
            return self.db.execute(query, params).rowcount

    # Cooldowns: a provider refused work (usage limit hit) and must not be asked again until `until`.
    def set_cooldown(self, provider: str, until: float, reason: str):
        with self.db:
            self.db.execute("INSERT INTO cooldowns (provider, until, reason) VALUES (?,?,?) "
                            "ON CONFLICT(provider) DO UPDATE SET until=excluded.until, reason=excluded.reason",
                            (provider, until, reason))

    def cooldown(self, provider: str, now: float | None = None) -> dict | None:
        row = self.db.execute("SELECT * FROM cooldowns WHERE provider=?", (provider,)).fetchone()
        now = time.time() if now is None else now
        return dict(row) if row and row["until"] > now else None

    # Marks: when the room last did a once-only thing for a project (notified the owner of #N, opened a scheduled
    # issue), so it isn't done twice (commonroom#151).
    def mark(self, project: str | None, key: str, at: float | None = None):
        with self.db:
            self.db.execute("INSERT INTO marks (project, key, at) VALUES (?,?,?) "
                            "ON CONFLICT(project, key) DO UPDATE SET at=excluded.at",
                            (project or "", key, time.time() if at is None else at))

    def marked(self, project: str | None, key: str) -> float | None:
        row = self.db.execute("SELECT at FROM marks WHERE project=? AND key=?", (project or "", key)).fetchone()
        return row["at"] if row else None
