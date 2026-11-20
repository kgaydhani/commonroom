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

from commonroom import handshake, registry
from commonroom.dispatch import fill_way, read_way
from commonroom.ledger import Ledger

SESSION_START = read_way(handshake.TEMPLATES / "session-start.md")
EVENT = "SessionStart"
LIMIT = 2.0             # seconds: a hook still working after this adds nothing
WORKER_WINDOW = 86400   # a folder a room worker ran in within the last day is a worker's folder
SENDER_CHARS = 40


def _senders(notes: list[dict]) -> str:
    counts = Counter((" ".join(str(n["from"]).split())[:SENDER_CHARS] if n["from"] else "unsigned") for n in notes)
    return ", ".join(f"{who} ({k})" if k > 1 else who for who, k in counts.most_common())


def context(data: dict, now: float | None = None) -> str:
    """The text to add for one SessionStart input, or "" to add nothing."""
    now = time.time() if now is None else now
    cwd = data.get("cwd")
    if os.environ.get("COMMONROOM_WORKER") or not isinstance(cwd, str) or not os.path.isdir(cwd):
        return ""
    reg = registry.load(check=False)
    if not reg.projects:
        return ""
    ledger = Ledger.reader()
    try:
        if ledger is not None and any(handshake._inside(cwd, d) for d in ledger.worker_dirs(now - WORKER_WINDOW)):
            return ""   # a room worker: its prompt already carries the record
        try:
            p = handshake.find(reg, cwd=Path(cwd))
        except handshake.HandshakeError:
            return ""
        marks = ledger.marks(p.name, handshake.ATTACHED) if ledger is not None else []
    finally:
        if ledger is not None:
            ledger.close()
    where = handshake.folder_of(p)
    seen = max((at for _, at in marks), default=0.0)   # `room attach` shows every note, so later ones are unread
    unread = [n for n in handshake.read_notes(where) if n["at"] > seen]
    count = f"{len(unread)}, from {_senders(unread)}" if unread else "0"
    text = fill_way(SESSION_START, project=p.name, digest=str(where / "digest.md"), notes=count,
                    reply=handshake.reply_line(p))
    if not unread and not handshake.charter_written(where, p.name):
        return text.split("\n", 1)[0]
    return text


def main(stdin=None, stdout=None, limit: float | None = None) -> int:
    """Read the hook's JSON, write the added context (or nothing), exit 0 whatever happens."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    found: list[str] = []

    def work():
        try:
            data = json.loads(stdin.read() or "null")
            if isinstance(data, dict) and data.get("hook_event_name", EVENT) == EVENT:
                found.append(context(data))
        except Exception:   # noqa: BLE001 - the hook must never fail a session
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(LIMIT if limit is None else limit)
    if found and found[0]:
        try:
            stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": found[0]}}))
            stdout.flush()
        except Exception:   # noqa: BLE001
            pass
    return 0


def cmd_hook(args) -> int:
    """`room hook session-start`: the plugin's SessionStart hook. Any other event adds nothing."""
    return main() if args.event == "session-start" else 0
