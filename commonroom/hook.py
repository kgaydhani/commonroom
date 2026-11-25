"""The desk, part 1 (commonroom#262): a Claude Code SessionStart hook that points a session at its project's room folder.

It sits beside Claude Code, never between (axiom A0): its only output is `hookSpecificOutput.additionalContext`, never
a decision, a rewrite or a permission. In a registered project's folder, or a git worktree of one, it adds one short
paragraph from commonroom/ways/handshake/session-start.md (axiom A1): where the project's digest is, how many notes in
its inbox are unread and who they are from, and how to reply. With no unread note and no charter, only the template's
first line. In a known place (commonroom#571: a folder in places.json, no project) it adds one short line from
place-start.md. Elsewhere, with an empty registry, in a room worker's folder, or on any error, it prints nothing and exits
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
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from commonroom import handshake, places, policy, registry, sessions
from commonroom.dispatch import fill_way, read_way
from commonroom.ledger import Ledger

SESSION_START = read_way(handshake.TEMPLATES / "session-start.md")
COLD_RETURN = read_way(handshake.TEMPLATES / "cold-return.md")
COMPACTED = read_way(handshake.TEMPLATES / "compacted.md")
PLACE_START = read_way(handshake.TEMPLATES / "place-start.md")
CARD = "card"           # the `room` subcommand that shows a place; named in the place line once it exists
EVENT = "SessionStart"
PROMPT = "UserPromptSubmit"
TAIL = 524288           # bytes: only the end of a transcript is read, for its last assistant turn
HEAD = 4194304         # bytes: the most of a rollout's first line that is read to see what it is
CHUNK = 1048576        # bytes read per step when counting compaction boundaries
BOUNDARY = re.compile(rb'"subtype"\s*:\s*"compact_boundary"')
OVERLAP = 64
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


def _is_rollout(path) -> bool:
    """True when the file's first line is a Codex `session_meta` line (detected from its content, not its path)."""
    try:
        with open(path, "rb") as handle:
            first = json.loads(handle.readline(HEAD))
        return isinstance(first, dict) and first.get("type") == "session_meta"
    except (OSError, TypeError, ValueError):
        return False


def codex_turn(raw: bytes) -> tuple[float, int] | None:
    """(time, context tokens) of one Codex rollout line if it is a `token_count` line with usage, else None. The
    context is `last_token_usage.input_tokens`, which already includes cached input. No message text is read
    (commonroom#398)."""
    try:
        line = json.loads(raw)
        payload = line["payload"]
        if line["type"] != "event_msg" or payload["type"] != "token_count":
            return None
        tokens = payload["info"]["last_token_usage"]["input_tokens"]
        at = datetime.fromisoformat(line["timestamp"].replace("Z", "+00:00"))
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at.timestamp(), tokens
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def last_turn(path, size: int = TAIL) -> tuple[float, int] | None:
    """(time, context tokens) of the last assistant turn in the tail of the transcript at `path`, or None. No message
    text is kept: it reads sessions._parse's fields only (commonroom#367), or for a Codex rollout the last
    `token_count` line's (commonroom#398)."""
    codex = _is_rollout(path)
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            handle.seek(max(0, end - size))
            lines = handle.read().split(b"\n")
    except (OSError, TypeError, ValueError):
        return None
    for raw in reversed(lines):
        if codex:
            turn = codex_turn(raw)
            if turn is not None:
                return turn
            continue
        parsed = sessions._parse(raw)
        if parsed is not None:
            return parsed[0], parsed[6]
    return None


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} minutes" if minutes < 120 else f"{minutes // 60} hours"


def _brief_line(p, ledger: Ledger | None) -> str:
    """One sentence naming the project's newest untaken handoff brief, or the command that writes one."""
    brief = next((path for path in reversed(handshake.handoffs(p))
                  if ledger is None or ledger.marked(p.name, handshake.TOOK + path.name) is None), None)
    if brief:
        return f"The project's newest untaken handoff brief is `handoffs/{brief.name}`."
    return f"None is written: `room handoff {p.name} --session <id> --reason <text>` writes one."


def compactions(path, budget: float = LIMIT / 2) -> int:
    """How many `compact_boundary` lines the transcript at `path` holds, counted by scanning it in chunks; 0 when it
    cannot be read, and what was counted so far when `budget` seconds run out (commonroom#410)."""
    stop = time.monotonic() + budget
    count, carry = 0, b""
    try:
        with open(path, "rb") as handle:
            while time.monotonic() < stop:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                data = carry + chunk
                count += len(BOUNDARY.findall(data))
                # a match cut by the chunk edge is counted in the next pass; one already counted is not repeated
                last = [m.end() for m in BOUNDARY.finditer(data)]
                carry = data[max(len(data) - OVERLAP, last[-1] if last else 0):]
    except (OSError, TypeError, ValueError):
        return 0
    return count


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
        if p is None:
            return ""
        named = _brief_line(p, ledger)
    finally:
        if ledger is not None:
            ledger.close()
    return fill_way(COLD_RETURN, gap=_ago(now - turn[0]), context=f"{turn[1] // 1000}k", brief=named)


def place_line(data: dict) -> str:
    """One short line for a folder that is a known place (and no project), or "" for any other folder or worker."""
    cwd = data.get("cwd")
    if os.environ.get("COMMONROOM_WORKER") or not isinstance(cwd, str) or not os.path.isdir(cwd):
        return ""
    place = places.find(places.load(), cwd)
    if place is None:
        return ""
    name = Path(place["path"]).name or place["path"]
    described = " ".join(str(place.get("description") or "").split())
    cli = sys.modules.get("commonroom.cli")
    card = f" `room {CARD}` shows it." if cli is not None and hasattr(cli, f"cmd_{CARD}") else ""
    return fill_way(PLACE_START, place=name, description=f": {described.rstrip('.')}" if described else "", card=card)


def context(data: dict, now: float | None = None) -> str:
    """The text to add for one SessionStart input, or "" to add nothing."""
    now = time.time() if now is None else now
    ledger = Ledger.reader()
    try:
        p = _project(data, now, ledger)
        if p is None:
            return place_line(data)
        unread = handshake.unread(handshake.read_notes(handshake.folder_of(p)), p, ledger)
        brief = handshake.offer(p, ledger, data.get("session_id"))
        path = data.get("transcript_path")
        n = compactions(path) if data.get("source") == "compact" and isinstance(path, str) else 0
        named = _brief_line(p, ledger) if n >= 2 else ""
    finally:
        if ledger is not None:
            ledger.close()
    where = handshake.folder_of(p)
    # `unread` is by this project: only its own attach or `room notes` reads, never another's (#269)
    count = f"{len(unread)}, from {_senders(unread)}" if unread else "0"
    text = fill_way(SESSION_START, project=p.name, digest=str(where / "digest.md"), notes=count,
                    reply=handshake.reply_line(p))
    if not unread and not handshake.charter_written(where, p.name):
        text = text.split("\n", 1)[0]
    if named:
        text = f"{text}\n" + fill_way(COMPACTED, count=str(n), brief=named)
    return f"{text}\n{brief}" if brief else text


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
