"""What "landed" means for a project (commonroom#246). Unset, a merge. `[progress] landed` is an argv list run in the
project's folder; each non-empty line it prints is one thing that landed since `{since}`."""
from __future__ import annotations

import datetime
import subprocess
import sys
from dataclasses import dataclass, field

TIMEOUT = 60


@dataclass
class Landed:
    count: int | None = None          # None: no answer (unset, or the command gave none), so merges decide
    lines: list[str] = field(default_factory=list)
    why: str = ""                     # why there is no answer, when a command was set


def declared(cfg: dict) -> list | None:
    command = (cfg.get("progress") or {}).get("landed")
    return list(command) if command else None


def since_text(since: float) -> str:
    return datetime.datetime.fromtimestamp(since, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def landed(cfg: dict, since: float, timeout: float = TIMEOUT) -> Landed:
    """Run the project's `landed` command. A failure, unreadable output or a timeout gives no answer, never progress."""
    command = declared(cfg)
    if not command:
        return Landed()
    argv = [sys.executable if p == "{python}" else str(p).replace("{since}", since_text(since)) for p in command]
    try:
        done = subprocess.run(argv, cwd=cfg.get("_root"), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return Landed(why=f"the landed command ran past {timeout:g} s")
    except (OSError, subprocess.SubprocessError) as exc:
        return Landed(why=f"the landed command could not run: {type(exc).__name__}: {exc}")
    if done.returncode != 0:
        return Landed(why=f"the landed command exited {done.returncode}")
    try:
        text = done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return Landed(why="the landed command printed output that is not readable text")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return Landed(count=len(lines), lines=lines)
