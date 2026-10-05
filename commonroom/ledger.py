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
    note TEXT
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
"""


def default_path() -> Path:
    home = Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom")
    return home / "room.sqlite3"


class Ledger:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

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
                  now: float | None = None) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute(
                "INSERT INTO runs (id, project, task, role, provider, model, effort, autonomous, started_at, status, session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, project, task, role, provider, model, effort, int(autonomous),
                 time.time() if now is None else now, "running", session_id))
        return run_id

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
                    finished: bool = False) -> list[dict]:
        clauses, params = [], []
        if project is not None:
            clauses.append("project=?"); params.append(project)
        if autonomous is not None:
            clauses.append("autonomous=?"); params.append(int(autonomous))
        if finished:
            clauses.append("status!='running'")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.execute(f"SELECT * FROM runs {where} ORDER BY started_at DESC LIMIT ?", (*params, limit)).fetchall()
        result = []
        for r in rows:
            row = dict(r)
            row["outputs"] = json.loads(row["outputs"] or "[]")
            result.append(row)
        return result

    def task_failures(self, project: str | None, task: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM runs WHERE project IS ? AND task=? AND status='failed'",
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
