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

Codex's session records (`$CODEX_HOME/sessions`, else `~/.codex/sessions`, one JSONL per session, found by walking the
folder) go into the same rows. Its `token_count` events carry a running total for the session, so a row holds the
growth between one total and the next, put on the UTC day of the event that grew it; a repeated total adds nothing
and a total that falls is not counted again. Codex's input includes its cached part, so `input` is its input minus
cached input (cached and uncached kept apart), `cache_read` is cached input, `cache_write` its cache-write count, and
reasoning output stays inside `output` (Codex's own total is input plus output). The model is the latest
`turn_context` model, the folder is the last part of the session's working directory, and only counts, models,
times and that folder name are read. A missing Codex folder is not an error.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from commonroom import sessions

FILENAME = "token-rows.jsonl"
FIELDS = ("input", "output", "cache_write", "cache_read")


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def rows_file() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom") / FILENAME


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


def _stamp(text) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _codex_file(path: Path, totals: dict) -> None:
    """Add one Codex session record's per-day growth to `totals`."""
    session, folder, model = path.stem, "codex", "unknown"
    seen = [0, 0, 0, 0]       # highest running total so far: input (uncached), output, cache write, cache read
    try:
        handle = open(path, encoding="utf-8")
    except OSError:
        sessions.stats["unreadable"] += 1
        return
    with handle:
        while True:
            try:
                raw = handle.readline()
            except (OSError, UnicodeDecodeError):
                sessions.stats["unreadable"] += 1
                return
            if not raw:
                return
            try:
                obj = json.loads(raw)
                kind, body = obj.get("type"), obj.get("payload") or {}
                if kind == "session_meta":
                    session = str(body.get("id") or session)
                    folder = Path(str(body.get("cwd") or "codex").replace("\\", "/")).name or "codex"
                elif kind == "turn_context":
                    model = str(body.get("model") or model)
                elif kind == "event_msg" and body.get("type") == "token_count":
                    use = (body.get("info") or {}).get("total_token_usage")
                    at = _stamp(obj.get("timestamp"))
                    if not use or at is None:
                        continue
                    cached = int(use.get("cached_input_tokens") or 0)
                    now = [max(0, int(use.get("input_tokens") or 0) - cached), int(use.get("output_tokens") or 0),
                           int(use.get("cache_write_input_tokens") or 0), cached]
                    delta = [max(0, a - b) for a, b in zip(now, seen)]
                    seen = [max(a, b) for a, b in zip(now, seen)]
                    if any(delta):
                        day = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d")
                        t = totals.setdefault((session, model, day), {"folder": folder, "first": at, "last": at,
                                                                      "counts": [0, 0, 0, 0]})
                        t["first"], t["last"] = min(t["first"], at), max(t["last"], at)
                        t["counts"] = [a + b for a, b in zip(t["counts"], delta)]
            except (ValueError, AttributeError, TypeError):
                sessions.stats["bad"] += 1


def _codex(root: Path, totals: dict) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.jsonl")):
        _codex_file(path, totals)


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def collect(projects_dir: Path | None = None, out: Path | None = None, cache: Path | None = None,
            codex: Path | None = None) -> dict:
    """Append the rows the transcripts hold that the rows file does not. Returns {"added", "bad", "unreadable"}:
    rows appended, lines that could not be parsed and files that could not be opened (all skipped, none raised)."""
    out = out or rows_file()
    done = _written(out)
    new = []
    totals = _totals(projects_dir or sessions.default_dir(), cache)
    _codex(codex or codex_dir(), totals)
    for (session, model, day), t in sorted(totals.items(),
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
