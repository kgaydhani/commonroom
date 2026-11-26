"""The desk, part 1 (commonroom#262): a Claude Code SessionStart hook that points a session at its project's room folder.

It sits beside Claude Code, never between (axiom A0): its only output is `hookSpecificOutput.additionalContext`, never
a decision, a rewrite or a permission. In a registered project's folder, or a git worktree of one, it adds one short
paragraph from commonroom/ways/handshake/session-start.md (axiom A1): where the project's digest is, how many notes in
its inbox are unread and who they are from, and how to reply. With no unread note and no charter, only the template's
first line. In a known place (commonroom#571: a folder in places.json, no project) it adds one short line from
place-start.md, then the other places used in the last fortnight, one line each (commonroom#578; a project's text is unchanged). Elsewhere, with an empty registry, in a room worker's folder, or on any error, it prints nothing and exits
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
HOME_START = read_way(handshake.TEMPLATES / "home-start.md")
PLACES_RECENT = read_way(handshake.TEMPLATES / "places-recent.md")
FRESH_START = read_way(handshake.TEMPLATES / "fresh-start.md")
OFFERED = "session-offers.json"   # in the room home: the sessions already offered a fresh start
SCANS = "session-scans.json"   # in the room home: how far each session's transcript has been read
OFFER_KEEP = 90 * 86400          # seconds an offered session is remembered
_CWD_VALUE = re.compile(rb'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')
_STAMP_VALUE = re.compile(rb'"timestamp"\s*:\s*("[^"]*")')
ANSWER_BACK = read_way(handshake.TEMPLATES / "answer-back.md")
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
ANSWERS_PER_PROMPT = 2  # answers added to one prompt
LINE_CHARS = 160        # the most of one line in the places list


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


def _span(path, state: dict, budget: float = LIMIT / 2) -> dict:
    """The scan of a transcript, carried on from `state` ({"off": bytes read, "first": time of its first timestamp or None,
    "dirs": the distinct folders its lines name}): only the bytes added since then are read, keeping no text, and only whole
    lines count. Starts over if the file got shorter. Stops early when `budget` seconds run out, and the next call goes on."""
    off, first, dirs = state.get("off", 0), state.get("first"), set(state.get("dirs", []))
    stop = time.monotonic() + budget
    with open(path, "rb") as handle:
        if os.fstat(handle.fileno()).st_size < off:
            off, first, dirs = 0, None, set()
        handle.seek(off)
        for raw in handle:
            if not raw.endswith(b"\n") or time.monotonic() > stop:
                break
            off += len(raw)
            if first is None and (m := _STAMP_VALUE.search(raw)):
                try:
                    first = datetime.fromisoformat(json.loads(m.group(1))).timestamp()
                except (ValueError, OverflowError):
                    pass
            if (m := _CWD_VALUE.search(raw)):
                try:
                    if isinstance(cwd := json.loads(m.group(1)), str) and cwd:
                        dirs.add(sessions.path_key(cwd))
                except ValueError:
                    pass
    return {"off": off, "first": first, "dirs": sorted(dirs)}


def _records(name: str, now: float) -> dict:
    """The small record file `name` in the room home, entries older than OFFER_KEEP dropped; {} if missing or corrupt."""
    try:
        seen = json.loads(places.path_file().with_name(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(seen, dict):
        return {}
    return {k: v for k, v in seen.items() if isinstance(v, dict) and isinstance(v.get("at"), (int, float)) and now - v["at"] < OFFER_KEEP}


def _store(name: str, seen: dict) -> None:
    path = places.path_file().with_name(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(seen, sort_keys=True), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _offered(session: str, now: float, record: bool = False) -> bool:
    """Whether `session` was already offered a fresh start; with `record`, notes it as offered (a write to a small file in
    the room home, beside the places file)."""
    seen = _records(OFFERED, now)
    if session in seen or not record:
        return session in seen
    _store(OFFERED, seen | {session: {"at": now}})
    return False


def fresh_start(data: dict, now: float | None = None) -> str:
    """The text to add for one UserPromptSubmit input, or "": the session began more than `fresh_start_days` days ago or
    has worked in more than `fresh_start_places` folders, and has not been told so yet (commonroom#580). It offers
    once per session and never blocks (axiom A0); it works in any folder, not only in a project's. Each prompt reads only the
    transcript bytes added since the last one (state in SCANS, kept until the session is offered)."""
    now = time.time() if now is None else now
    path, session = data.get("transcript_path"), data.get("session_id")
    if os.environ.get("COMMONROOM_WORKER") or not isinstance(path, str) or not isinstance(session, str) or not session:
        return ""
    if _offered(session, now):
        return ""
    scans = _records(SCANS, now)
    before = scans.get(session, {})
    state = _span(path, before)
    if state != {k: before.get(k) for k in state}:
        _store(SCANS, scans | {session: state | {"at": now}})
    began, count = state["first"], len(state["dirs"])
    days, limit = policy.DEFAULTS["fresh_start_days"], policy.DEFAULTS["fresh_start_places"]
    if began is not None and now - began > days * 86400:
        why = f"{int((now - began) // 86400)} days old"
    elif count > limit:
        why = f"{count} folders wide"
    else:
        return ""
    ledger = Ledger.reader()
    try:
        p = _project(data, now, ledger)
    finally:
        if ledger is not None:
            ledger.close()
    command = f"room handoff {p.name if p else '<project>'} --session {session} --reason <text>"
    _offered(session, now, record=True)
    scans.pop(session, None)
    _store(SCANS, scans)
    return fill_way(FRESH_START, why=why, command=command)


def answers_back(data: dict) -> str:
    """The answers to this session's questions (`room route answer`, commonroom#577), one short line each, each delivered
    once. At most ANSWERS_PER_PROMPT, each at most ANSWER_CHARS long, so it fits the hook's text budget; the rest wait for the next prompt. Never in a worker."""
    session = data.get("session_id")
    if os.environ.get("COMMONROOM_WORKER") or not isinstance(session, str) or not session:
        return ""
    reader = Ledger.reader()   # read-only first: most prompts have no answer, and the hook never creates a ledger
    if reader is None:
        return ""
    try:
        if not reader.waiting_answers(session, 1):
            return ""
    finally:
        reader.close()
    ledger = Ledger()
    try:
        rows = ledger.take_answers(session, ANSWERS_PER_PROMPT)
    finally:
        ledger.close()
    return "\n".join(fill_way(ANSWER_BACK, place=r["to_place"], question=r["summary"][:LINE_CHARS // 2], answer=r["answer"])
                     for r in rows)


def prompt_context(data: dict, parts: list | None = None) -> str:
    """What the prompt hook adds: the cold-return offer, the fresh-start offer and any answers back, each failing alone.
    Each text is added to `parts` as soon as it is ready, so one that runs out of time loses only itself."""
    parts = [] if parts is None else parts
    for part in (cold_return, fresh_start, answers_back):
        try:
            parts.append(part(data))
        except Exception:   # noqa: BLE001
            pass
    return "\n".join(p for p in parts if p)


def place_line(data: dict) -> str:
    """One short line for a folder that is a known place (and no project), or "" for any other folder or worker."""
    cwd = data.get("cwd")
    if os.environ.get("COMMONROOM_WORKER") or not isinstance(cwd, str) or not os.path.isdir(cwd):
        return ""
    place = places.find(places.load(), cwd)
    if place is None:
        return ""
    name = Path(place["path"]).name or place["path"]
    if place.get("home"):
        line = HOME_START.strip()
        return f"{line}\n{recent}" if (recent := recent_places(place, line)) else line
    described = " ".join(str(place.get("description") or "").split())
    line = fill_way(PLACE_START, place=name, description=f": {described.rstrip('.')}" if described else "")
    return f"{line}\n{recent}" if (recent := recent_places(place, line)) else line


def recent_places(here: dict | None, room_line: str = "", now: float | None = None) -> str:
    """The list of places used in the last `places_days` days, newest first, at most `places_listed`, one line each
    (commonroom#578): the name, the path and the description. The place the session is in, and folders gone from
    disk, are left out; so are the oldest if `room_line` and the list would pass `session_start_max_tokens`. "" when
    none, or when the count is 0."""
    limit = policy.DEFAULTS["places_listed"]
    since = (time.time() if now is None else now) - policy.DEFAULTS["places_days"] * 86400
    lines = []
    for row in places.listing(places.load()):
        if len(lines) >= limit:
            break
        if row["last"] < since or not row["exists"] or (here is not None and row["path"] == here["path"]):
            continue
        said = " ".join(str(row["description"] or "").split()).rstrip(" .")
        lines.append((f"- `{row['name']}` ({row['path']})" + (f": {said}" if said else ""))[:LINE_CHARS])
    budget = policy.DEFAULTS["session_start_max_tokens"] * 4 - len(room_line)   # characters, as tokens are chars / 4
    while lines and len(fill_way(PLACES_RECENT, days=str(policy.DEFAULTS["places_days"]), places="\n".join(lines))) + 1 > budget:
        lines.pop()                       # over the hook's budget: the oldest go first
    return fill_way(PLACES_RECENT, days=str(policy.DEFAULTS["places_days"]), places="\n".join(lines)) if lines else ""


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
    text = f"{text}\n{brief}" if brief else text
    here = places.find(places.load(), p.path) or {"path": str(p.path)}   # the project's own folder is left out
    return f"{text}\n{recent}" if (recent := recent_places(here, text)) else text


def main(stdin=None, stdout=None, limit: float | None = None, event: str = EVENT) -> int:
    """Read the hook's JSON, write the added context (or nothing), exit 0 whatever happens."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    found: list[str] = []
    pieces: list[str] = []   # prompt texts as they are ready: what is done survives a later one running out of time

    def work():
        try:
            data = json.loads(stdin.read() or "null")
            if isinstance(data, dict) and data.get("hook_event_name", event) == event:
                found.append(context(data) if event == EVENT else prompt_context(data, pieces))
        except Exception:   # noqa: BLE001 - the hook must never fail a session
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(LIMIT if limit is None else limit)
    text = found[0] if found else "\n".join(list(pieces))
    if text:
        try:
            stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))
            stdout.flush()
        except Exception:   # noqa: BLE001
            pass
    return 0


def cmd_hook(args) -> int:
    """`room hook session-start` and `room hook prompt-submit`: the plugin's hooks. Any other event adds nothing."""
    events = {"session-start": EVENT, "prompt-submit": PROMPT}
    return main(event=events[args.event]) if args.event in events else 0
