"""The owner's own Claude Code sessions, as spans of activity per registered project (commonroom#239).

Claude Code writes every session's transcript under `~/.claude/projects/<encoded folder>/`, on every surface: the
session's own `<session id>.jsonl`, and each subagent's `<session id>/subagents/agent-*.jsonl` beside it. A subagent's
transcript sits under its parent session's folder even when it works in a worktree elsewhere. Each line carries a
timestamp, the session id and the `cwd`; each assistant line carries the call's token counts.

A line belongs to the registered project whose folder equals its `cwd` or holds it, compared as real paths and
case-insensitively on Windows; a line in no registered project's folder is the owner's own (project None). The
encoded folder name never decides it: it turns `/` and `.` into `-`, so a project's own worktree
(`<project>/.claude/worktrees/x`) and a sibling folder (`<project>-wt/...`) encode alike. Every folder is opened,
because the owner's work elsewhere must share a meter step by time with the projects' (a step during his unregistered
work is his, not whichever project also had a session open); only files modified since the span began are read.
Lines of room workers (their folder or session id is in the ledger) are left out: those are runs already.

Read-only and text-free: of each line it reads only the timestamp, the session id, the `cwd`, the message id, the
model and the usage numbers. It never returns or stores message text. The model of each call says which weekly window a
session spends (commonroom#256): a plan's top model has its own, smaller one.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

# Assistant messages of one session less than this apart are one span of activity; a longer quiet is a pause.
GAP = 15 * 60
# A message's timestamp is when its call ended; the call itself ran for some seconds before it. Each span starts this
# long before its first message, so a lone message is a short span and not an instant that no meter gap can hold.
LEAD = 60
_ASSISTANT = re.compile(rb'"type"\s*:\s*"assistant"')
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# What a call sent the model: its context. Every call of a long session re-reads it.
CONTEXT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def default_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def path_key(path) -> str:
    """A path as compared here: real, absolute and case-folded where the file system is (Windows)."""
    return os.path.normcase(os.path.realpath(str(path)))


def _within(path: str, roots) -> str | None:
    """The longest of `roots` (path keys) that equals `path` or holds it, or None."""
    best = None
    for root in roots:
        if (path == root or path.startswith(root.rstrip(os.sep) + os.sep)) and (best is None or len(root) > len(best)):
            best = root
    return best


def _transcripts(projects_dir: Path, since: float):
    """Transcript files modified at or after `since`, in every folder: each session's own file and its subagents'.
    A file not modified since then holds no line from then, so it is never opened."""
    try:
        entries = sorted(projects_dir.iterdir())
    except OSError:
        return
    for folder in entries:
        if not folder.is_dir():
            continue
        for path in sorted([*folder.glob("*.jsonl"), *folder.glob("*/subagents/*.jsonl")]):
            try:
                if path.stat().st_mtime >= since:
                    yield path
            except OSError:
                continue


def _parse(raw: bytes):
    """(timestamp, session id, cwd, message id, tokens, model, context tokens) of one transcript line, or None when it
    is not an assistant line that reports usage, is malformed, or lacks any of the first five. The model is None when
    the line names none."""
    if not _ASSISTANT.search(raw):
        return None
    try:
        line = json.loads(raw)
        if line.get("type") != "assistant":
            return None
        message = line["message"]
        counts = message["usage"]
        tokens = sum(int(counts.get(key) or 0) for key in TOKEN_KEYS)
        context = sum(int(counts.get(key) or 0) for key in CONTEXT_KEYS)
        at = datetime.fromisoformat(line["timestamp"]).timestamp()
        session, cwd = line["sessionId"], line["cwd"]
        if not (isinstance(session, str) and isinstance(cwd, str) and session and cwd):
            return None
        mid, model = message.get("id"), message.get("model")
        return (at, session, cwd, mid if isinstance(mid, str) else None, tokens,
                model if isinstance(model, str) and model else None, context)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def _read_from(path: Path, offset: int):
    """(messages, new offset, bytes read) of the complete lines of `path` from byte `offset`. A last line with no
    newline yet is still being written: it is not consumed, and the offset stays before it. Read line by line, in
    bytes, so a large transcript is never held whole."""
    found, read = [], 0
    try:
        handle = open(path, "rb")
    except OSError:
        return found, offset, read
    with handle:
        handle.seek(offset)
        for raw in handle:
            read += len(raw)
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            parsed = _parse(raw)
            if parsed is not None:
                found.append(parsed)
    return found, offset, read


# --- the cache: per transcript, how far it was read and the parsed calls so far (never any text) ---------------------
CACHE_VERSION = 2          # 2: each call also holds its model and context (commonroom#256)
# Bytes read from transcripts by the last `spans` call; for timing and tests.
stats = {"bytes": 0, "files": 0}


def cache_file() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom") / "sessions-cache.json"


def _valid(entry) -> bool:
    """Whether a cached file entry has the shape this reader wrote. Anything else means the cache is not trusted."""
    try:
        return (isinstance(entry["size"], int) and isinstance(entry["mtime"], float) and isinstance(entry["offset"], int)
                and all(isinstance(m, list) and len(m) == 7 and isinstance(m[0], float) and isinstance(m[1], str)
                        and isinstance(m[2], str) and (m[3] is None or isinstance(m[3], str)) and isinstance(m[4], int)
                        and (m[5] is None or isinstance(m[5], str)) and isinstance(m[6], int)
                        for m in entry["calls"]))
    except (KeyError, TypeError):
        return False


def _load_cache(path: Path) -> dict:
    """path key -> {"size", "mtime", "offset", "calls": [[at, session, cwd, message id, tokens, model, context], ...]}. A missing or
    corrupt cache is an empty one: the reader then reads in full and rebuilds it."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data["files"]
        if data["v"] != CACHE_VERSION or not isinstance(files, dict) or not all(_valid(e) for e in files.values()):
            return {}
        return files
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _save_cache(path: Path, files: dict) -> None:
    """Write beside, then replace atomically: two processes updating at once leave one whole file or the other."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"v": CACHE_VERSION, "files": files}, out, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _messages(path: Path, files: dict, key: str) -> list:
    """The file's parsed calls, read only from where the cache left off. `files` is updated in place."""
    try:
        st = path.stat()
    except OSError:
        return []
    entry = files.get(key)
    if entry and (st.st_size < entry["offset"] or st.st_mtime < entry["mtime"]):
        entry = None                                        # shrank, or replaced by something older: read it again
    base = entry["calls"] if entry else []
    offset = entry["offset"] if entry else 0
    if entry and st.st_size == entry["size"] and st.st_mtime == entry["mtime"]:
        calls = base
    else:
        new, offset, read = _read_from(path, offset)
        stats["bytes"] += read
        stats["files"] += 1
        calls = base + [list(m) for m in new]
    files[key] = {"size": st.st_size, "mtime": st.st_mtime, "offset": offset, "calls": calls}
    return calls


def spans(projects: dict, start: float, end: float, exclude_dirs=(), exclude_sessions=(),
          projects_dir: Path | None = None, gap: float = GAP, cache: Path | None = None) -> list[dict]:
    """The owner's own sessions between `start` and `end`, as spans of activity per project.

    `projects` maps a project's key to its folder. `exclude_dirs` are folders where room workers ran and
    `exclude_sessions` their session ids: their lines are left out. Each span is {"project", "session_id",
    "started_at", "ended_at", "messages", "tokens", "models", "context", "folder"}: one session's (and its subagents') assistant messages in one
    project, each less than `gap` seconds after the one before. The project is None for the owner's sessions in no
    registered project's folder. `models` maps each model the span's calls named to their tokens (a call that names none
is under None), `context` is the largest context one call sent, and `folder` is the `cwd` of its latest call. Oldest
first. With no project, nothing is read.

    Only the bytes added to each transcript since the last call are read: what was parsed so far is kept in `cache`
    (default: sessions-cache.json in the room home). The answer is the same as a full read."""
    if not projects:
        return []
    roots = {path_key(folder): key for key, folder in projects.items()}
    workers = [path_key(folder) for folder in exclude_dirs]
    skip = set(exclude_sessions)
    owners: dict = {}          # cwd -> (a room worker's?, project key or None)
    found: dict = {}
    cache = cache or cache_file()
    before = _load_cache(cache)
    files: dict = {}
    stats.update(bytes=0, files=0)
    for path in _transcripts(projects_dir or default_dir(), start):
        key = path_key(path)
        if key in before:
            files[key] = before[key]
        for at, session, cwd, message_id, tokens, model, context in _messages(path, files, key):
            if not start <= at <= end or session in skip:
                continue
            if cwd not in owners:
                real = path_key(cwd)
                owners[cwd] = (_within(real, workers) is not None, roots.get(_within(real, roots)))
            worker, project = owners[cwd]
            if worker:
                continue
            times, calls = found.setdefault((project, session), ([], {}))
            times.append((at, cwd))
            call = message_id if message_id is not None else len(calls)   # one call is written once per content block
            first_at, most, _, widest = calls.get(call, (at, 0, None, 0))
            calls[call] = (min(first_at, at), max(most, tokens), model, max(widest, context))
    if files != before:
        _save_cache(cache, files)
    result = []
    for (project, session), (times, calls) in sorted(found.items(), key=lambda item: (item[0][0] or "", item[0][1])):
        times.sort()
        runs, first, (last, folder) = [], times[0][0], times[0]
        for at, cwd in times[1:]:
            if at - last >= gap:
                runs.append((first, last, folder))
                first = at
            last, folder = at, cwd
        runs.append((first, last, folder))
        for first, last, folder in runs:
            inside = [call for call in calls.values() if first <= call[0] <= last]
            models: dict = {}
            for _, tokens, model, _ in inside:
                models[model] = models.get(model, 0) + tokens
            result.append({"project": project, "session_id": session, "started_at": max(start, first - LEAD),
                           "ended_at": last, "messages": len(inside), "tokens": sum(c[1] for c in inside),
                           "models": models, "context": max((c[3] for c in inside), default=0), "folder": folder})
    result.sort(key=lambda s: (s["started_at"], s["project"] or "", s["session_id"]))
    return result
