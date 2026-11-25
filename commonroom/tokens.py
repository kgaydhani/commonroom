"""Per-session, per-model token rows kept from Claude Code's transcripts (commonroom#641).

Claude Code deletes its session records after about 30 days, so `room tokens collect` copies the counts into the room
home: `token-rows.jsonl`, one JSON line per session, per model, per UTC day, with the first and last time seen, the
session id, the project folder (the transcript folder's own name), the model and the four counts (input, output, cache
write, cache read). Counts, models and times only: no message text, tool input or file content is read into a row.

It reuses `sessions`' reader and cache (every transcript on disk, whatever its age, subagents included). A call is
written once per content block, so calls are deduplicated on message id, keeping the largest counts. Rows are append
only: each run totals what the transcripts hold now, compares it with the sum of the rows already written for that
key, and appends one row with the difference when a count grew. A grown transcript so adds only its new counts, and a
run with nothing new adds nothing. Consumers sum the rows of a key.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from commonroom import sessions

FILENAME = "token-rows.jsonl"
FIELDS = ("input", "output", "cache_write", "cache_read")


def rows_file() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom") / FILENAME


def read_rows(path: Path | None = None) -> list[dict]:
    """Every whole row in the rows file (a line that is not a whole row is ignored)."""
    out = []
    try:
        text = (path or rows_file()).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            row = json.loads(line)
            row["day"], row["model"]
            out.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _written(path: Path) -> dict:
    """key -> counts already written. A line that is not a whole row is ignored."""
    done: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return done
    for line in text.splitlines():
        try:
            row = json.loads(line)
            key = (row["session"], row["model"], row["day"])
            counts = [int(row[f]) for f in FIELDS]
        except (ValueError, KeyError, TypeError):
            continue
        have = done.setdefault(key, [0, 0, 0, 0])
        for i, n in enumerate(counts):
            have[i] += n
    return done


def _totals(projects_dir: Path, cache: Path | None) -> dict:
    """(session, model, day) -> {"folder", "first", "last", "counts"} of every transcript under `projects_dir`."""
    files = sessions._load_cache(cache or sessions.cache_file())
    before = dict(files)
    sessions.stats.update(bytes=0, files=0, bad=0, unreadable=0)
    calls: dict = {}          # (session, message id or unique) -> [first at, folder, model, counts]
    for path in sessions._transcripts(projects_dir, 0.0):
        key = sessions.path_key(path)
        folder = path.relative_to(projects_dir).parts[0]
        for n, (at, session, _, mid, _, model, _, counts) in enumerate(sessions._messages(path, files, key)):
            if model == "<synthetic>":
                continue
            call = (session, mid if mid is not None else f"{key}#{n}")
            old = calls.get(call)
            if old is None:
                calls[call] = [at, folder, model or "unknown", list(counts)]
            else:
                old[0] = min(old[0], at)
                old[3] = [max(a, b) for a, b in zip(old[3], counts)]
    if files != before:
        sessions._save_cache(cache or sessions.cache_file(), files)
    totals: dict = {}
    for (session, _), (at, folder, model, counts) in calls.items():
        day = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d")
        t = totals.setdefault((session, model, day), {"folder": folder, "first": at, "last": at, "counts": [0, 0, 0, 0]})
        t["first"], t["last"] = min(t["first"], at), max(t["last"], at)
        t["counts"] = [a + b for a, b in zip(t["counts"], counts)]
    return totals


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def collect(projects_dir: Path | None = None, out: Path | None = None, cache: Path | None = None) -> dict:
    """Append the rows the transcripts hold that the rows file does not. Returns {"added", "bad", "unreadable"}:
    rows appended, lines that could not be parsed and files that could not be opened (all skipped, none raised)."""
    out = out or rows_file()
    done = _written(out)
    new = []
    for (session, model, day), t in sorted(_totals(projects_dir or sessions.default_dir(), cache).items(),
                                           key=lambda item: (item[1]["first"], item[0])):
        have = done.get((session, model, day), [0, 0, 0, 0])
        delta = [max(0, a - b) for a, b in zip(t["counts"], have)]
        if any(delta):
            new.append({"day": day, "session": session, "folder": t["folder"], "model": model,
                        "first": _iso(t["first"]), "last": _iso(t["last"]), **dict(zip(FIELDS, delta))})
    if new:
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in new))
    return {"added": len(new), "bad": sessions.stats["bad"], "unreadable": sessions.stats["unreadable"]}
