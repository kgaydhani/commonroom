"""The owner's own Claude Code sessions, as spans of activity per registered project (commonroom#239).

Claude Code writes every session's transcript under `~/.claude/projects/<encoded folder>/`, on every surface: the
session's own `<session id>.jsonl`, and each subagent's `<session id>/subagents/agent-*.jsonl` beside it. A subagent's
transcript sits under its parent session's folder even when it works in a worktree elsewhere. Each line carries a
timestamp, the session id and the `cwd`; each assistant line carries the call's token counts.

A line belongs to the registered project whose folder equals its `cwd` or holds it, compared as real paths and
case-insensitively on Windows. The encoded folder name only chooses which folders to open: it turns `/` and `.` into
`-`, so a project's own worktree (`<project>/.claude/worktrees/x`) and a sibling folder (`<project>-wt/...`) encode
alike. Lines of room workers (their folder or session id is in the ledger) are left out: those are runs already.

Read-only and text-free: of each line it reads only the timestamp, the session id, the `cwd`, the message id and the
usage numbers. It never returns or stores message text.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from commonroom import usage

# Assistant messages of one session less than this apart are one span of activity; a longer quiet is a pause.
GAP = 15 * 60
# A message's timestamp is when its call ended; the call itself ran for some seconds before it. Each span starts this
# long before its first message, so a lone message is a short span and not an instant that no meter gap can hold.
LEAD = 60
_ASSISTANT = re.compile(rb'"type"\s*:\s*"assistant"')
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


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


def _transcripts(projects_dir: Path, folders: tuple, since: float):
    """Transcript files in the folders whose name starts with one of `folders` (lower-cased encodings), modified at or
    after `since`: each session's own file and its subagents'."""
    try:
        entries = sorted(projects_dir.iterdir())
    except OSError:
        return
    for folder in entries:
        if not folder.is_dir() or not folder.name.lower().startswith(folders):
            continue
        for path in sorted([*folder.glob("*.jsonl"), *folder.glob("*/subagents/*.jsonl")]):
            try:
                if path.stat().st_mtime >= since:
                    yield path
            except OSError:
                continue


def _messages(path: Path):
    """(timestamp, session id, cwd, message id, tokens) of each assistant line that reports usage. A malformed line,
    or one missing any of these, is skipped. Read line by line, in bytes, so a large transcript is never held whole."""
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        for raw in handle:
            if not _ASSISTANT.search(raw):
                continue
            try:
                line = json.loads(raw)
                if line.get("type") != "assistant":
                    continue
                message = line["message"]
                counts = message["usage"]
                tokens = sum(int(counts.get(key) or 0) for key in TOKEN_KEYS)
                at = datetime.fromisoformat(line["timestamp"]).timestamp()
                session, cwd = line["sessionId"], line["cwd"]
                if not (isinstance(session, str) and isinstance(cwd, str) and session and cwd):
                    continue
                yield at, session, cwd, message.get("id"), tokens
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue


def spans(projects: dict, start: float, end: float, exclude_dirs=(), exclude_sessions=(),
          projects_dir: Path | None = None, gap: float = GAP) -> list[dict]:
    """The owner's own sessions between `start` and `end`, as spans of activity per project.

    `projects` maps a project's key to its folder. `exclude_dirs` are folders where room workers ran and
    `exclude_sessions` their session ids: their lines are left out. Each span is {"project", "session_id",
    "started_at", "ended_at", "messages", "tokens"}: one session's (and its subagents') assistant messages in one
    project, each less than `gap` seconds after the one before. Oldest first. With no project, nothing is read."""
    if not projects:
        return []
    roots = {path_key(folder): key for key, folder in projects.items()}
    workers = [path_key(folder) for folder in exclude_dirs]
    skip = set(exclude_sessions)
    folders = tuple(usage.session_folder(folder).lower() for folder in projects.values())
    owners: dict = {}
    found: dict = {}
    for path in _transcripts(projects_dir or default_dir(), folders, start):
        for at, session, cwd, message_id, tokens in _messages(path):
            if not start <= at <= end or session in skip:
                continue
            if cwd not in owners:
                real = path_key(cwd)
                owners[cwd] = None if _within(real, workers) else roots.get(_within(real, roots))
            project = owners[cwd]
            if project is None:
                continue
            times, calls = found.setdefault((project, session), ([], {}))
            times.append(at)
            call = message_id if message_id is not None else len(calls)   # one call is written once per content block
            first_at, most = calls.get(call, (at, 0))
            calls[call] = (min(first_at, at), max(most, tokens))
    result = []
    for (project, session), (times, calls) in sorted(found.items()):
        times.sort()
        runs, first, last = [], times[0], times[0]
        for at in times[1:]:
            if at - last >= gap:
                runs.append((first, last))
                first = at
            last = at
        runs.append((first, last))
        for first, last in runs:
            inside = [tokens for at, tokens in calls.values() if first <= at <= last]
            result.append({"project": project, "session_id": session, "started_at": max(start, first - LEAD),
                           "ended_at": last, "messages": len(inside), "tokens": sum(inside)})
    result.sort(key=lambda s: (s["started_at"], s["project"], s["session_id"]))
    return result
