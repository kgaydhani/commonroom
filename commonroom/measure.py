"""A project's measure (commonroom#324): an optional `[measure]` command that prints the project's own number.
The room runs it on a cadence, records the reading in the ledger and shows the latest in the digest. It never judges the number."""
from __future__ import annotations

import subprocess
import time

from commonroom.ledger import Ledger

TIMEOUT = 60
LINE_MAX = 200
SHOWN = 5


def settings(cfg: dict) -> dict | None:
    """The project's `[measure]` table with defaults, or None when it declares no command."""
    table = cfg.get("measure") or {}
    command = str(table.get("command") or "").strip()
    if not command:
        return None
    try:
        every = float(table.get("every_hours", 24))
    except (TypeError, ValueError):
        every = 24.0
    return {"command": command, "every_hours": every, "label": str(table.get("label") or "measure")}


def _run(command: str, cwd, bash: str | None) -> tuple[int, str]:
    argv = [bash, "-lc", command] if bash else command
    done = subprocess.run(argv, cwd=cwd, shell=not bash, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=TIMEOUT)
    return done.returncode, done.stdout


def take(ledger: Ledger, project: str | None, cfg: dict, cwd, now: float | None = None, bash: str | None = None,
         run=_run) -> dict | None:
    """Run the command if one is declared and `every_hours` have passed since the last reading; record the reading.
    A failed or timed-out command is a failed reading, never an exception. Returns the reading, or None if none was due."""
    cfg_m = settings(cfg)
    if not cfg_m:
        return None
    now = time.time() if now is None else now
    last = ledger.readings(project, 1)
    if last and now - last[0]["at"] < cfg_m["every_hours"] * 3600:
        return None
    try:
        code, out = run(cfg_m["command"], cwd, bash)
        lines = (out or "").strip().splitlines()
        text = lines[0].strip()[:LINE_MAX] if lines else ""
    except subprocess.TimeoutExpired:
        code, text = -1, f"timed out after {TIMEOUT}s"
    except Exception as exc:
        code, text = -1, f"{type(exc).__name__}: {exc}"[:LINE_MAX]
    ledger.record_reading(project, cfg_m["label"], code, text, now)
    return {"at": now, "label": cfg_m["label"], "exit_code": code, "line": text}
