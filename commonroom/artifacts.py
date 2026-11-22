"""A project's published artifacts (commonroom#214): an optional `[artifacts]` table maps a name to the repo paths
(globs allowed) it is built from. A headless run can't republish an artifact, so the digest says which ones are behind
the project's default branch, and `room.py artifact published <name>` clears one. Nothing is judged or published here."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from commonroom.ledger import Ledger

BRANCH = "origin/main"
TIMEOUT = 15
KEY = "artifact:"
SEP = "\x1f"


def declared(cfg: dict) -> dict[str, list[str]]:
    """The project's `[artifacts]` as name -> paths; entries with no usable path are dropped."""
    table = cfg.get("artifacts")
    out: dict[str, list[str]] = {}
    for name, paths in (table.items() if isinstance(table, dict) else []):
        if isinstance(paths, str):
            paths = [paths]
        if isinstance(paths, list) and (kept := [str(x) for x in paths if str(x).strip()]):
            out[str(name)] = kept
    return out


def newest(root: Path, paths: list[str]) -> tuple[str, float, str] | None:
    """(short sha, commit time, subject) of the newest commit on the default branch touching `paths`, or None if none.
    Raises OSError or subprocess.SubprocessError if git can't answer."""
    done = subprocess.run(["git", "log", "-1", f"--format=%h{SEP}%ct{SEP}%s", BRANCH, "--", *paths], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
    if done.returncode != 0:
        lines = (done.stderr or "").strip().splitlines()
        raise OSError(lines[-1] if lines else f"git exit {done.returncode}")
    line = done.stdout.strip()
    if not line:
        return None
    sha, at, subject = line.split(SEP, 2)
    return sha, float(at), subject


def stale(ledger: Ledger, project: str | None, cfg: dict, root: Path) -> str | None:
    """The digest section's body: one line per artifact behind the default branch; None when nothing is stale or the
    project declares none. A git failure becomes one line, never an exception."""
    want = declared(cfg)
    if not want:
        return None
    lines = []
    for name, paths in want.items():
        try:
            found = newest(root, paths)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return f"- artifacts: could not read git history ({str(exc).strip() or type(exc).__name__})"
        if found is None:
            continue
        sha, at, subject = found
        marked = ledger.marked(project, KEY + name)
        if marked is None or at > marked:
            lines.append(f"- {name}: {sha} {subject} ({'never published' if marked is None else 'changed since last published'}). "
                         f"After republishing, run `room.py artifact published {name}`.")
    return "\n".join(lines) or None


def publish(ledger: Ledger, project: str | None, cfg: dict, name: str, now: float | None = None) -> bool:
    """Record `name` as published now. False when the project doesn't declare it."""
    if name not in declared(cfg):
        return False
    ledger.mark(project, KEY + name, at=time.time() if now is None else now)
    return True
