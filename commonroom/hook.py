"""The desk, part 1 (commonroom#262): a Claude Code SessionStart hook that points a session at its project's room folder.

It sits beside Claude Code, never between (axiom A0): its only output is `hookSpecificOutput.additionalContext`, never
a decision, a rewrite or a permission. In a registered project's folder, or a git worktree of one, it adds one short
paragraph from commonroom/ways/handshake/session-start.md (axiom A1): where the project's digest is, how many notes in
its inbox are unread and who they are from, and how to reply. With no unread note and no charter, only the template's
first line. Elsewhere, with an empty registry, in a room worker's folder, or on any error, it prints nothing and exits
0. It calls no model and no network: its one subprocess is handshake.find's `git rev-parse`, it opens the ledger
read-only, and it gives up after LIMIT seconds.

The contract, from https://code.claude.com/docs/en/hooks (SessionStart): the hook's JSON arrives on stdin with
`hook_event_name`, `session_id`, `cwd` and `source`. One JSON object on stdout whose
`hookSpecificOutput.additionalContext` is a string adds that string to the session's context; SessionStart has no
blocking or decision control.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from commonroom import handshake, policy, registry, sessions
from commonroom.dispatch import fill_way, read_way
from commonroom.ledger import Ledger

SESSION_START = read_way(handshake.TEMPLATES / "session-start.md")
COLD_RETURN = read_way(handshake.TEMPLATES / "cold-return.md")
EVENT = "SessionStart"
PROMPT = "UserPromptSubmit"
TAIL = 524288           # bytes: only the end of a transcript is read, for its last assistant turn
LIMIT = 2.0             # seconds: a hook still working after this adds nothing
WORKER_WINDOW = 86400   # a folder a room worker ran in within the last day is a worker's folder
SENDER_CHARS = 40


def _senders(notes: list[dict]) -> str:
    counts = Counter((" ".join(str(n["from"]).split())[:SENDER_CHARS] if n["from"] else "unsigned") for n in notes)
    return ", ".join(f"{who} ({k})" if k > 1 else who for who, k in counts.most_common())


def _project(data: dict, now: float, ledger: Ledger | None):
    """The registered project a hook's `cwd` is in, or None: a room worker, an unknown folder or an empty registry."""
    cwd = data.get("cwd")
    if os.environ.get("COMMONROOM_WORKER") or not isinstance(cwd, str) or not os.path.isdir(cwd):
        return None
    reg = registry.load(check=False)
    if not reg.projects:
        return None
    if ledger is not None and any(handshake._inside(cwd, d) for d in ledger.worker_dirs(now - WORKER_WINDOW)):
        return None   # a room worker: its prompt already carries the record
    try:
        return handshake.find(reg, cwd=Path(cwd))
    except handshake.HandshakeError:
        return None


def last_turn(path, size: int = TAIL) -> tuple[float, int] | None:
    """(time, context tokens) of the last assistant turn in the tail of the transcript at `path`, or None. No message
    text is kept: it reads sessions._parse's fields only (commonroom#367)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            handle.seek(max(0, end - size))
            lines = handle.read().split(b"\n")
    except (OSError, TypeError, ValueError):
        return None
    for raw in reversed(lines):
        parsed = sessions._parse(raw)
        if parsed is not None:
            return parsed[0], parsed[6]
    return None


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} minutes" if minutes < 120 else f"{minutes // 60} hours"


def cold_return(data: dict, now: float | None = None) -> str:
    """The text to add for one UserPromptSubmit input, or "" to add nothing: the last turn is older than the cache
    lifetime and its context is large (commonroom#367). It offers; it never blocks (axiom A0)."""
    now = time.time() if now is None else now
    path = data.get("transcript_path")
    if not isinstance(path, str):
        return ""
    turn = last_turn(path)
    if turn is None or now - turn[0] <= policy.DEFAULTS["cold_return_minutes"] * 60 \
            or turn[1] <= policy.DEFAULTS["cold_return_context"]:
        return ""
    ledger = Ledger.reader()
    try:
        p = _project(data, now, ledger)
    finally:
        if ledger is not None:
            ledger.close()
    if p is None:
        return ""
    brief = handshake.newest_handoff_file(handshake.folder_of(p))
    if brief:
        named = f"The project's newest handoff brief is `{brief}`."
    else:
        named = f"None is written: `room handoff {p.name} --session <id> --reason <text>` writes one."
    return fill_way(COLD_RETURN, gap=_ago(now - turn[0]), context=f"{turn[1] // 1000}k", brief=named)


def context(data: dict, now: float | None = None) -> str:
    """The text to add for one SessionStart input, or "" to add nothing."""
    now = time.time() if now is None else now
    ledger = Ledger.reader()
    try:
        p = _project(data, now, ledger)
        if p is None:
            return ""
        unread = handshake.unread(handshake.read_notes(handshake.folder_of(p)), p, ledger)
    finally:
        if ledger is not None:
            ledger.close()
    where = handshake.folder_of(p)
    # `unread` is by this project: only its own attach or `room notes` reads, never another's (#269)
    count = f"{len(unread)}, from {_senders(unread)}" if unread else "0"
    text = fill_way(SESSION_START, project=p.name, digest=str(where / "digest.md"), notes=count,
                    reply=handshake.reply_line(p))
    if not unread and not handshake.charter_written(where, p.name):
        return text.split("\n", 1)[0]
    return text


def main(stdin=None, stdout=None, limit: float | None = None, event: str = EVENT) -> int:
    """Read the hook's JSON, write the added context (or nothing), exit 0 whatever happens."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    found: list[str] = []

    def work():
        try:
            data = json.loads(stdin.read() or "null")
            if isinstance(data, dict) and data.get("hook_event_name", event) == event:
                found.append((context if event == EVENT else cold_return)(data))
        except Exception:   # noqa: BLE001 - the hook must never fail a session
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(LIMIT if limit is None else limit)
    if found and found[0]:
        try:
            stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": found[0]}}))
            stdout.flush()
        except Exception:   # noqa: BLE001
            pass
    return 0


def cmd_hook(args) -> int:
    """`room hook session-start` and `room hook prompt-submit`: the plugin's hooks. Any other event adds nothing."""
    events = {"session-start": EVENT, "prompt-submit": PROMPT}
    return main(event=events[args.event]) if args.event in events else 0
