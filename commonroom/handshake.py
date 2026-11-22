"""The handshake, first cut (commonroom#253): one folder per registered project that any agent can be pointed at.

The folder is `<room home>/projects/<name>`, or the project's `shared` folder from the registry, so no project's own
repo is touched. It holds four plain files: `README.md` (what the folder is), `charter.md` (what the owner wants, in
the owner's words: created once with empty headings, never written again), `digest.md` (the project's state, built
here in code) and `inbox/` (one new file per note: nothing edits or deletes a note, so two writers never collide). An
agent with no room installed uses the folder with no command: it reads `digest.md` and adds a file to `inbox/`.

No model and no GitHub call: the commands read the registry, the ledger and the folder, and write only the folder.
The words the folder's files open with come from commonroom/ways/handshake/ (axiom A1).
"""
from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from commonroom import artifacts, config as config_mod, ledger as ledger_mod, measure, metrics, progress as progress_mod, registry, trials
from commonroom.waytext import fill_way, read_way
from commonroom.ledger import Ledger

TEMPLATES = Path(__file__).resolve().parent / "ways" / "handshake"
README, CHARTER, DIGEST, REPLY, HANDOFF, DECISIONS, DECISIONS_MORE = (
    read_way(TEMPLATES / f"{name}.md") for name in ("README", "charter", "digest", "reply", "handoff", "decisions", "decisions-more"))
HANDOFF_OFFER = read_way(TEMPLATES / "handoff-offer.md")
WEEK = 7 * 86400
NEWEST = 5                 # notes shown in the digest; `room notes` shows them all
DECISIONS_SHOWN = 10       # standing decisions and bounds listed in the digest, however old (#365)
NOTE_CHARS = 800           # of each note's text in the digest
ATTACHED = "attached:"     # ledger mark key prefix: attached:<who>
KINDS = ("outcome", "bound", "decision", "suggestion")   # what an owner statement can be (design-v3); `direction` is separate
DIRECTION = "direction"   # ledger mark key: when the owner last changed a project's direction (`note --direction`)
READ = "read:"             # ledger mark key prefix: read:<project>, set when that project's own session read its inbox
HANDOFF_MARK = "handoff:"  # ledger mark key prefix: handoff:<session>, set when that session wrote a handoff (#363)
TOOK = "took:"             # ledger mark key prefix: took:<file>, set when a session took that handoff brief (#366)
ADD = "`room projects add <name> <folder> --share <percent>`"
UNITS = {"m": 60, "h": 3600, "d": 86400, "w": WEEK}
PARTS = ("README.md", "charter.md", "digest.md", "inbox")
MODES = {"open": "the planner may add work", "fixed": "only scheduled and explicitly added work",
         "paced": "it runs up to its share", "spare": "it runs only on allowance nobody else wants"}


class HandshakeError(Exception):
    """A command that can't go ahead; the message says why and what to do."""


def home() -> Path:
    return ledger_mod.default_path().parent


def folder_of(p: registry.Project) -> Path:
    return Path(p.shared) if p.shared else home() / "projects" / p.name


def _inside(child, parent) -> bool:
    c, p = os.path.normcase(os.path.abspath(child)), os.path.normcase(os.path.abspath(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def load_registry() -> registry.Registry:
    """The registry, read without its rules (another project's missing folder must not block this one)."""
    path = registry.default_path()
    try:
        reg = registry.load(path, check=False)
    except registry.RegistryError as exc:
        raise HandshakeError(f"refused: {exc}") from None
    if not reg.projects:
        raise HandshakeError(f"no projects are registered in {path}. Add one with {ADD}.")
    return reg


def main_repo(cwd) -> Path | None:
    """The main repository when `cwd` is in a git worktree (or any checkout), else None: no git, or not a repo."""
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(cwd), capture_output=True, text=True,
                             timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    common = Path(out) if os.path.isabs(out) else Path(cwd) / out
    return common.resolve().parent if common.name == ".git" else None


def find(reg: registry.Registry, name: str | None = None, cwd: Path | None = None) -> registry.Project:
    """The project named, or else the one whose folder (or room folder) holds `cwd`, the deepest if several do."""
    names = ", ".join(sorted(reg.projects))
    if name:
        match = reg.projects.get(name) or next((p for n, p in reg.projects.items() if n.lower() == name.lower()), None)
        if match is None:
            raise HandshakeError(f"no project named {name!r} is registered (registered: {names}).")
        return match
    cwd = Path.cwd() if cwd is None else cwd
    here = [cwd]
    if (root := main_repo(cwd)) is not None:   # a worktree belongs to the project its main repository is registered as
        here.append(root)
    found = [(len(os.path.abspath(where)), p) for p in reg.projects.values()
             for where in (p.path, folder_of(p)) if isinstance(where, (str, Path)) and str(where)
             and any(_inside(at, where) for at in here)]
    if not found:
        raise HandshakeError(f"{cwd} is in no registered project's folder (registered: {names}). Name a project, "
                             f"or register this folder with {ADD}.")
    return max(found, key=lambda pair: pair[0])[1]


def _write(path: Path, text: str):
    """Write atomically (a temporary file, then a replace) with LF line endings."""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def prepare(p: registry.Project) -> Path:
    """Make the folder and its parts where missing. The README and the charter are only ever created, never rewritten."""
    where = folder_of(p)
    (where / "inbox").mkdir(parents=True, exist_ok=True)
    for name, way in (("README.md", README), ("charter.md", CHARTER)):
        try:
            with open(where / name, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(fill_way(way, project=p.name) + "\n")
        except FileExistsError:
            pass
    return where


def _one_line(value: str) -> str:
    return " ".join(str(value).split())


def write_note(where: Path, text: str, sender: str, to: str | None = None, now: float | None = None,
               kind: str | None = None) -> Path:
    """One new file in `inbox/`, never an existing one: a name taken (two notes in one instant) moves on a microsecond,
    so names still sort by time."""
    stamp = datetime.fromtimestamp(time.time() if now is None else now)
    head = ["---", f"from: {_one_line(sender)}", *([f"to: {_one_line(to)}"] if to else []),
            f"at: {stamp:%Y-%m-%dT%H:%M:%S}", *([f"kind: {_one_line(kind)}"] if kind else []), "---", ""]
    body = "\n".join(head) + "\n" + text.replace("\r\n", "\n").strip("\n") + "\n"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", sender).strip("-")[:40] or "note"
    inbox = where / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    while True:
        path = inbox / f"{stamp:%Y%m%d-%H%M%S-%f}-{slug}.md"
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
            return path
        except FileExistsError:
            stamp += timedelta(microseconds=1)


def read_notes(where: Path) -> list[dict]:
    """Every file in `inbox/`, newest first. Front matter (`---` lines around `from:`, `to:`, `at:`) is optional: a
    note without it is dated by its file time, and one that isn't text is still listed, by name, with text None."""
    inbox, notes = where / "inbox", []
    if not inbox.is_dir():
        return notes
    for path in inbox.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        note = {"name": path.name, "at": path.stat().st_mtime, "from": None, "to": None, "text": None, "dated": False, "kind": None}
        notes.append(note)
        try:
            text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.split("\n")
        ends = [i for i, line in enumerate(lines) if line.strip() == "---"]
        if len(ends) >= 2 and ends[0] == 0:
            fields = dict((k.strip().lower(), v.strip()) for k, sep, v in
                          (line.partition(":") for line in lines[1:ends[1]]) if sep)
            note["from"], note["to"] = fields.get("from") or None, fields.get("to") or None
            note["kind"] = fields.get("kind") or None
            try:
                note["at"], note["dated"] = datetime.fromisoformat(fields.get("at", "")).timestamp(), True
            except ValueError:
                pass
            text = "\n".join(lines[ends[1] + 1:])
        note["text"] = text.strip()
    return sorted(notes, key=lambda n: (int(n["at"]), n["name"]), reverse=True)


def parse_since(text: str, now: float) -> float:
    """`6h`, `30m`, `3d`, `1w` back from now, or a local date or time such as `2026-11-20` or `2026-11-20 14:00`."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([mhdw])", text.strip())
    if match:
        return now - float(match.group(1)) * UNITS[match.group(2)]
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except ValueError:
        raise HandshakeError(f"--since {text!r}: give a time such as 6h, 3d, 2026-11-20 or '2026-11-20 14:00'.") from None


def _when(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts):%a %d %b %H:%M}"


def note_heading(n: dict) -> str:
    kind = f" ({n['kind']})" if n["kind"] else ""
    who = (f", from {n['from']}{kind}" if n["from"] else (f", {kind.strip()}" if kind else "")) + (f" to {n['to']}" if n["to"] else "")
    if n["text"] is None:
        return f"{_when(n['at'])} (file time){who}, `inbox/{n['name']}`: not readable as text"
    return f"{_when(n['at'])}{'' if n['dated'] else ' (file time)'}{who}, `inbox/{n['name']}`"


def room_toml(p: registry.Project) -> dict:
    """The project's own room.toml over the defaults, or {} when it can't be read."""
    try:
        return config_mod.load(Path(p.path) / "room.toml", follow_host=False)
    except (OSError, TypeError, tomllib.TOMLDecodeError):
        return {}


def ledger_name(p: registry.Project) -> str:
    """The name the ledger records the project's runs under: its room.toml `[project] name`, else its registry name."""
    return (room_toml(p).get("project") or {}).get("name") or p.name


def _body(text: str) -> str:
    """A charter's text without its `# ` title line."""
    lines = text.strip().split("\n")
    return "\n".join(lines[1:] if text.lstrip().startswith("# ") else lines).strip()


def charter_written(where: Path, name: str) -> bool:
    """True when `charter.md` holds more than the headings the room created it with."""
    try:
        text = (where / "charter.md").read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return False
    return _body(text) not in ("", _body(fill_way(CHARTER, project=name)))


def reply_line(p: registry.Project) -> str:
    """How to reply to the project's folder, from `reply.md`: shared by the digest and the session hook."""
    return fill_way(REPLY, project=p.name, folder=str(folder_of(p)))


def _charter(where: Path, p: registry.Project) -> str:
    try:
        text = (where / "charter.md").read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except FileNotFoundError:
        return "There is no `charter.md` in this folder."
    if _body(text) == _body(fill_way(CHARTER, project=p.name)):
        cfg = room_toml(p)
        mission, goal = (cfg.get("project") or {}).get("mission"), (cfg.get("goal") or {}).get("milestone")
        lines = ["Not written yet: `charter.md` holds only its headings."]
        if mission:
            lines += ["Until it is, here is the mission from the project's room.toml:", "",
                      "\n".join(f"> {line}".rstrip() for line in str(mission).strip().split("\n"))]
        if goal:
            lines += ["", f"Its current goal is the milestone \"{goal}\"."]
        return "\n".join(lines)
    return re.sub(r"(?m)^(#+ )", r"#\1", _body(text)) or "Not written yet: `charter.md` is empty."


def _share(reg: registry.Registry, p: registry.Project) -> str:
    share = registry.fmt_number(p.share) if registry.is_number(p.share) else repr(p.share)
    mode = "; ".join(f"`{m}`: {MODES[m]}" if m in MODES else f"`{m}`" for m in (p.work, p.when))
    strict = " Its unused share is never lent to other projects." if p.strict is True else ""
    return (f"{share}% of the owner's weekly allowance ({mode}).{strict} The owner's own share "
            f"is {registry.fmt_number(reg.owner_share())}%. Set in `{registry.default_path()}`.")


def _attached(ledger: Ledger, p: registry.Project) -> str:
    marks = ledger.marks(p.name, ATTACHED)
    return "\n".join(f"- {who}, last {_when(at)}" for who, at in marks) or "Nobody has run `room attach` here yet."


def _direction(ledger: Ledger, p: registry.Project, where: Path) -> str:
    """The digest's `Latest direction` line (with its blank line after): empty when no direction was ever recorded."""
    at = ledger.marked(p.name, DIRECTION)
    if at is None:
        return ""
    texts = [n for n in read_notes(where) if n["kind"] == "direction" and n["text"] and int(n["at"]) <= int(at) + 1]
    text = _one_line(texts[0]["text"])[:200] if texts else "(the note file is gone)"
    return f"Latest direction: {_when(at)}: {text}\n\n"


def _notes(where: Path) -> str:
    notes = read_notes(where)
    if not notes:
        return "None yet."
    parts = []
    for n in notes[:NEWEST]:
        parts.append(f"- {note_heading(n)}")
        if n["text"]:
            text = n["text"] if len(n["text"]) <= NOTE_CHARS else n["text"][:NOTE_CHARS].rstrip() + " [cut: see the file]"
            parts.append("\n".join(f"  > {line}".rstrip() for line in text.split("\n")))
    if len(notes) > NEWEST:
        parts.append(f"\n{len(notes) - NEWEST} older note(s) are in `inbox/`.")
    return "\n".join(parts)


def _decisions(where: Path, p: registry.Project) -> str:
    """The digest's section of every `decision` and `bound` note, newest first, so an owner statement never falls out
    with the newest-5 window. Empty (no section at all) when there are none."""
    found = [n for n in read_notes(where) if n["kind"] in ("decision", "bound")]
    if not found:
        return ""
    lines = []
    for n in found[:DECISIONS_SHOWN]:
        first = next((_one_line(line) for line in (n["text"] or "").split(chr(10)) if line.strip()), "(not readable as text)")
        first = first if len(first) <= NOTE_CHARS else first[:NOTE_CHARS].rstrip() + " [cut: see the file]"
        lines.append(f"- {_when(n['at'])}, {n['from'] or 'unknown sender'} ({n['kind']}): {first}")
    if len(found) > DECISIONS_SHOWN:
        lines.append(fill_way(DECISIONS_MORE, count=len(found) - DECISIONS_SHOWN, project=p.name))
    return fill_way(DECISIONS, entries=chr(10).join(lines))


LANDED_SHOWN = 10   # newest landings the digest lists; the rest are counted, so the digest stays small (A10)


def _work(ledger: Ledger, p: registry.Project, now: float) -> str:
    rows = ledger.runs_since(ledger_name(p), now - WEEK)
    runs, landed = metrics.model_runs(rows, now - WEEK, now), metrics.landings(rows, now - WEEK, now)
    running = [r for r in rows if r["status"] == "running" and r["started_at"] > now - 7200]
    if not (runs or landed or running):
        return "The ledger shows nothing run or landed for this project in the last 7 days."
    lines = []
    if runs:
        roles = ", ".join(f"{n} {role}" for role, n in Counter(r["role"] for r in runs).most_common())
        failed = sum(r["status"] in ("failed", "blocked") for r in runs)
        lines.append(f"{len(runs)} runs ({roles}), {failed} of them failed or blocked.")
        cost = sum(r.get("cost_usd") or 0 for r in runs)
        if cost:
            lines.append(f"About ${cost:,.0f} of Claude use at API prices. That is an estimate: the plan is not billed "
                         f"per run, and Codex runs are not counted.")
    if running:
        lines.append("Running now: " + ", ".join(f"{r['role']} on {r['task'] or 'no item'}" for r in running) + ".")
    lines.append(f"Landed: {len(landed)}." if landed else "Nothing landed.")
    own = room_toml(p)
    if progress_mod.declared(own):
        answer = progress_mod.landed(own, now - 86400)
        lines.append("Landed by the project's own count in 24 h: "
                     + (str(answer.count) if answer.count is not None else f"no answer ({answer.why}); merges count") + ".")
    for r in sorted(landed, key=lambda r: r["started_at"], reverse=True)[:LANDED_SHOWN]:
        refs = ", ".join(o.get("ref") for o in r["outputs"] if o.get("ref"))
        lines.append(f"- {_when(r['started_at'])}: {r['task'] or 'no item'}{f' ({refs})' if refs else ''}")
    if len(landed) > LANDED_SHOWN:
        lines.append(f"{len(landed) - LANDED_SHOWN} older landings in the last 7 days; the ledger has them.")
    return "\n".join(lines)


BOARD_CAP = 8   # findings the digest shows; the rest are counted, so the digest stays small (A9, A10)
BOARD_FIRST = {"owner-overdue": 0, "blocked-on-nothing": 1}   # these lead; the rest follow in the order found


def _board(ledger: Ledger, p: registry.Project) -> str | None:
    """The steward's latest sweep of the project's board; None if it has never run (the digest then shows nothing new)."""
    name = ledger_name(p)
    if ledger.marked(name, ledger.SWEEP) is None:
        return None
    found = ledger.findings(name)
    if not found:
        return "Nothing stale."
    shown = sorted(found, key=lambda f: (BOARD_FIRST.get(f["invariant"], len(BOARD_FIRST)), f["since"], f["number"]))[:BOARD_CAP]
    lines = [f"- {f['detail']}" for f in shown]
    if len(found) > len(shown):
        lines.append(f"- and {len(found) - len(shown)} more (`room.py findings {name}` lists them all).")
    return "\n".join(lines)


def _measure(ledger: Ledger, p: registry.Project) -> str | None:
    """The project's last readings of its own number (commonroom#324), newest first; None if it has none."""
    found = ledger.readings(ledger_name(p), measure.SHOWN)
    if not found:
        return None
    lines = []
    for r in found:
        said = (r["line"] or "(no output)") if r["exit_code"] == 0 else f"FAILED (exit {r['exit_code']}) {r['line']}".rstrip()
        lines.append(f"- {datetime.fromtimestamp(r['at']):%a %d %b %H:%M}: {r['label']}: {said}")
    return "\n".join(lines)


def _trials(ledger: Ledger, p: registry.Project, now: float) -> str | None:
    """One line per active trial with its verdict (commonroom#353); None when none is declared."""
    reports = trials.report(trials.FILE, ledger.runs_since(ledger_name(p), 0), now, only_active=True)
    return "\n".join(f"- {trials.line(t, res)}" for t, res in reports) or None


def _fields(path: Path) -> dict:
    """The `key: value` front matter between a file's first two `---` lines; {} when it has none or can't be read."""
    try:
        lines = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").split("\n")
    except (OSError, UnicodeDecodeError):
        return {}
    ends = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(ends) < 2 or ends[0] != 0:
        return {}
    return dict((k.strip().lower(), v.strip()) for k, sep, v in (line.partition(":") for line in lines[1:ends[1]]) if sep)


def _newest_handoff(where: Path) -> str | None:
    """The digest's one line naming the newest file in `handoffs/` (#363); None when there is none."""
    folder = where / "handoffs"
    found = sorted(f for f in folder.glob("*.md") if f.is_file()) if folder.is_dir() else []
    if not found:
        return None
    fields = _fields(found[-1])
    try:
        at = _when(datetime.fromisoformat(fields.get("at", "")).timestamp())
    except ValueError:
        at = _when(found[-1].stat().st_mtime) + " (file time)"
    return f"- `handoffs/{found[-1].name}`, {at}, from session {fields.get('from_session') or '(not named)'}"


def build_digest(reg: registry.Registry, p: registry.Project, ledger: Ledger, now: float | None = None) -> str:
    now = time.time() if now is None else now
    where = folder_of(p)
    text = fill_way(DIGEST, project=p.name, reply=_direction(ledger, p, where) + reply_line(p), built=f"{datetime.fromtimestamp(now):%a %d %b %Y %H:%M} (local time)",
                    charter=_charter(where, p), share=_share(reg, p), attached=_attached(ledger, p),
                    decisions=_decisions(where, p), notes=_notes(where), work=_work(ledger, p, now)) + "\n"
    board = _board(ledger, p)
    if board:
        text = text + "\n" + f"## Board check, latest sweep\n{board}" + "\n"
    shown = _measure(ledger, p)
    if shown:
        text = text + "\n" + f"## Measure\n{shown}" + "\n"
    running = _trials(ledger, p, now)
    if running:
        text = text + "\n" + f"## Trials\n{running}" + "\n"
    newest = _newest_handoff(where)
    if newest:
        text = text + "\n" + f"## Newest handoff\n{newest}" + "\n"
    behind = artifacts.stale(ledger, ledger_name(p), room_toml(p), Path(p.path))
    return text + "\n" + f"## Artifacts to republish\n{behind}" + "\n" if behind else text


def refresh(reg: registry.Registry, p: registry.Project, now: float | None = None, who: str | None = None) -> str:
    """Make the folder, record `who` attached (if given), and rebuild `digest.md`. Returns the digest."""
    now = time.time() if now is None else now
    where = prepare(p)
    ledger = Ledger()
    try:
        if who:
            ledger.mark(p.name, ATTACHED + who, at=now)
        text = build_digest(reg, p, ledger, now)
    finally:
        ledger.close()
    _write(where / "digest.md", text)
    return text


def last_read(ledger: Ledger | None, p: registry.Project) -> float:
    """When the project itself last read its inbox: its own `room attach` (as itself, by name or from its folder) or a
    full `room notes` from its folder. An attach as someone else, or by another project, never counts."""
    if ledger is None:
        return 0.0
    mine = p.name.lower()
    seen = [at for who, at in ledger.marks(p.name, ATTACHED) if who.lower() == mine]
    return max(seen + [ledger.marked(p.name, READ + p.name) or 0.0])


def unread(notes: list[dict], p: registry.Project, ledger: Ledger | None) -> list[dict]:
    """The notes in the project's inbox newer than its own last read, not written by the project itself."""
    seen = int(last_read(ledger, p))   # a note is dated to the second: one in the read's own second counts as unread
    return [n for n in notes if n["at"] >= seen and (n["from"] or "").lower() != p.name.lower()]


def _here(reg: registry.Registry) -> registry.Project | None:
    """The project whose folder the current directory is in, or None."""
    try:
        return find(reg)
    except HandshakeError:
        return None


def _user() -> str:
    try:
        return getpass.getuser() or "someone"
    except (OSError, KeyError, ImportError):
        return "someone"


def _start(args) -> tuple[registry.Registry, registry.Project]:
    """The project named as an argument (`room digest alpha`) or with `--project` (the same name twice is fine)."""
    given = {n for n in (getattr(args, "name", None), getattr(args, "project", None)) if n}
    if len(given) > 1:
        raise HandshakeError(f"refused: two different projects named ({', '.join(sorted(given))}). Name one.")
    reg = load_registry()
    return reg, find(reg, next(iter(given), None))


def _run(command):
    def wrapped(args) -> int:
        try:
            return command(args)
        except HandshakeError as exc:
            print(exc, file=sys.stderr)
            return 1
    return wrapped


@_run
def cmd_attach(args) -> int:
    reg, p = _start(args)
    here = None if args.who else _here(reg)
    who = _one_line(args.who or (here.name if here is not None and here.name == p.name else _user()))
    where = folder_of(p)
    kept = (where / "README.md").exists()
    text = refresh(reg, p, who=who)
    names = sorted((e.name for e in where.iterdir() if not e.name.startswith(".")),
                   key=lambda n: (PARTS.index(n) if n in PARTS else len(PARTS), n.lower()))
    held = [n + ("/" if (where / n).is_dir() else "") for n in names]
    print(f"{p.name}: {where}\nHolds {', '.join(held)} ({len(read_notes(where))} notes in inbox/).")
    if kept:
        print("README.md was already there and is left as it is.")
    print(f"Attached as {who}.\n\n{text}", end="")
    return 0


@_run
def cmd_note(args) -> int:
    *named, text = args.words    # `note TEXT` or `note PROJECT TEXT`
    if len(named) > 1:
        raise HandshakeError("refused: give the note's text as one argument, in quotes.")
    args.name = named[0] if named else None
    text = sys.stdin.read() if text == "-" else text
    kind = getattr(args, "kind", None)
    if kind and args.direction:
        raise HandshakeError("refused: give --direction or --kind, not both.")
    if kind and kind not in KINDS:
        raise HandshakeError(f"refused: --kind must be one of {', '.join(KINDS)}.")
    if not text.strip():
        raise HandshakeError("refused: the note is empty.")
    reg, p = _start(args)
    sender = args.sender
    if not sender:
        try:
            here = find(reg)
        except HandshakeError:
            here = None
        sender = here.name if here is not None and here.name != p.name else _user()
    now = time.time()
    path = write_note(prepare(p), text, sender, args.to, now, "direction" if args.direction else kind)
    if args.direction:
        ledger = Ledger()
        try:
            ledger.mark(p.name, DIRECTION, now)
        finally:
            ledger.close()
    refresh(reg, p)
    print(f"note for {p.name} from {_one_line(sender)}: {path}")
    return 0


@_run
def cmd_notes(args) -> int:
    reg, p = _start(args)
    kind = getattr(args, "kind", None)
    if kind and kind not in KINDS + (DIRECTION,):
        raise HandshakeError(f"refused: --kind must be one of {', '.join(KINDS + (DIRECTION,))}.")
    notes = read_notes(folder_of(p))
    if kind:
        notes = [n for n in notes if n["kind"] == kind]
    here = _here(reg)
    if not args.since and not kind and here is not None and here.name == p.name:   # the project reading its own inbox, all of it
        ledger = Ledger()
        try:
            ledger.mark(p.name, READ + p.name)
        finally:
            ledger.close()
    if args.since:
        since = parse_since(args.since, time.time())
        notes = [n for n in notes if n["at"] >= since]
    if not notes:
        print(f"no notes for {p.name}" + (f" since {args.since}" if args.since else "") + f" in {folder_of(p) / 'inbox'}")
    for n in notes:
        print(note_heading(n))
        if n["text"]:
            print("\n".join(f"  {line}".rstrip() for line in n["text"].split("\n")))
        print()
    return 0


@_run
def cmd_findings(args) -> int:
    """Every finding of the project's latest steward sweep, uncapped (the digest shows only the first few)."""
    reg, p = _start(args)
    ledger = Ledger()
    try:
        found = ledger.findings(ledger_name(p))
    finally:
        ledger.close()
    print("\n".join(f"{f['invariant']}: {f['detail']}" for f in found) if found else f"no findings for {p.name}")
    return 0


@_run
def cmd_artifact(args) -> int:
    """`room artifact published <name>`: record that the artifact was republished just now."""
    reg, p = _start(args)
    cfg = room_toml(p)
    ledger = Ledger()
    try:
        if not artifacts.publish(ledger, ledger_name(p), cfg, args.artifact):
            names = ", ".join(artifacts.declared(cfg)) or "none"
            print(f"refused: {p.name} has no artifact named {args.artifact!r}. Configured: {names}.", file=sys.stderr)
            return 1
    finally:
        ledger.close()
    print(f"{p.name}: artifact {args.artifact} marked published")
    return 0


@_run
def cmd_digest(args) -> int:
    reg, p = _start(args)
    print(refresh(reg, p), end="")
    return 0


def _git(cwd, *argv) -> str | None:
    """`git <argv>` in `cwd`: its output, or None when git is missing or fails (not a repo, no commit yet)."""
    try:
        done = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.rstrip() if done.returncode == 0 else None


def write_handoff(reg: registry.Registry, p: registry.Project, ledger: Ledger, text: str, session: str, reason: str,
                  context: int | None = None, cwd=None, now: float | None = None) -> Path:
    """One new file `handoffs/<stamp>-<session>.md` in the project's room folder, never an existing one (#363). The part
    built here comes first (goal, open work, git); the session's own text follows. Records `handoff:<session>`."""
    if not text.strip():
        raise HandshakeError("refused: the session's text is empty. Give it on standard input or with --file.")
    session, reason = _one_line(session or ""), _one_line(reason or "")
    if not session or not reason:
        raise HandshakeError("refused: a handoff needs --session <id> and --reason <text>.")
    now, cwd = time.time() if now is None else now, Path.cwd() if cwd is None else Path(cwd)
    stamp = datetime.fromtimestamp(now)
    inside = _git(cwd, "rev-parse", "--is-inside-work-tree") == "true"
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") if inside else None
    commit = _git(cwd, "rev-parse", "--short", "HEAD") if inside else None
    head = ["---", f"from_session: {session}", f"at: {stamp:%Y-%m-%dT%H:%M:%S}", f"reason: {reason}",
            *([f"context: {context}"] if context is not None else []), *([f"branch: {branch}"] if branch else []),
            *([f"commit: {commit}"] if commit else []), "---", ""]
    goal = (room_toml(p).get("goal") or {}).get("milestone")
    work = [f"Goal milestone: \"{goal}\"." if goal else "No goal milestone is set in the project's room.toml.", "",
            _work(ledger, p, now)]
    if (board := _board(ledger, p)) is not None:
        work += ["", "Board check, latest sweep:", board]
    if inside:
        status = _git(cwd, "status", "--short") or ""
        code = (f"Branch `{branch or '(none)'}` at `{commit or '(no commit yet)'}`. "
                + (f"`git status --short`:\n```\n{status}\n```" if status else "The working tree is clean."))
    else:
        code = f"{cwd} is not in a git repository."
    body = "\n".join(head) + "\n" + fill_way(HANDOFF, session=session, at=f"{stamp:%a %d %b %Y %H:%M} (local time)",
                                            reason=reason, work="\n".join(work), code=code,
                                            text=text.replace("\r\n", "\n").strip("\n")) + "\n"
    folder = prepare(p) / "handoffs"
    folder.mkdir(exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", session).strip("-")[:40] or "session"
    while True:
        path = folder / f"{stamp:%Y%m%d-%H%M%S-%f}-{slug}.md"
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
            break
        except FileExistsError:
            stamp += timedelta(microseconds=1)
    ledger.mark(p.name, HANDOFF_MARK + session, at=now)
    return path


def handoffs(p: registry.Project) -> list[Path]:
    """The project's handoff briefs, oldest first."""
    folder = folder_of(p) / "handoffs"
    return sorted(f for f in folder.glob("*.md") if f.is_file()) if folder.is_dir() else []


def _stamp(fields: dict, path: Path) -> str:
    try:
        return _when(datetime.fromisoformat(fields.get("at", "")).timestamp())
    except ValueError:
        return _when(path.stat().st_mtime) + " (file time)"


def offer(p: registry.Project, ledger: Ledger | None, session) -> str | None:
    """The one line telling a session about the newest handoff brief nobody has taken (#366); None when there is none.
    Reads only: marks and files."""
    for path in reversed(handoffs(p)):
        if ledger is None or ledger.marked(p.name, TOOK + path.name) is None:
            fields = _fields(path)
            return fill_way(HANDOFF_OFFER, file=str(path), session=fields.get("from_session") or "(not named)",
                            at=_stamp(fields, path), me=_one_line(str(session or "")) or "<this session's id>")
    return None


def take_handoff(p: registry.Project, ledger: Ledger, name: str, session: str, now: float | None = None) -> Path:
    """Record that `session` took the brief `name`: the mark `took:<file>` and the lineage from the brief's front matter."""
    session = _one_line(session or "")
    if not session:
        raise HandshakeError("refused: taking a handoff needs --session <id>.")
    path = Path(name)
    if not path.is_file():
        path = folder_of(p) / "handoffs" / path.name
    if not path.is_file() or path.parent.resolve() != (folder_of(p) / "handoffs").resolve():
        raise HandshakeError(f"refused: {name} is not a handoff brief of {p.name}. `room handoff list {p.name}` shows them.")
    fields = _fields(path)
    now = time.time() if now is None else now
    if not ledger.record_lineage(p.name, path.name, fields.get("from_session", ""), session, fields.get("reason", ""),
                                 fields.get("context", ""), fields.get("branch", ""), fields.get("commit", ""), at=now):
        by = ledger.lineage(p.name, path.name)[0]
        raise HandshakeError(f"refused: {path.name} was already taken by session {by['new_session']} ({_when(by['at'])}).")
    ledger.mark(p.name, TOOK + path.name, at=now)
    return path


def handoff_lines(p: registry.Project, ledger: Ledger) -> list[str]:
    """One line per handoff brief, newest first: from, to (or "not taken"), the reason and the time."""
    out = []
    for path in reversed(handoffs(p)):
        fields = _fields(path)
        got = ledger.lineage(p.name, path.name)
        out.append(f"{_stamp(fields, path)}, from {fields.get('from_session') or '(not named)'} to "
                   f"{got[0]['new_session'] if got else 'not taken'}: {fields.get('reason', '')} (`{path.name}`)")
    return out


def _handoff_sub(args) -> int:
    """`room handoff take <file> --session <id>` and `room handoff list [project]` (#366)."""
    verb, rest = args.name, list(args.rest)
    if verb == "take":
        if len(rest) != 1:
            raise HandshakeError("refused: `room handoff take <file> --session <id>` takes one file.")
        args.name, file = None, rest[0]
    else:
        if len(rest) > 1:
            raise HandshakeError("refused: `room handoff list [project]` takes at most one project.")
        args.name, file = (rest[0] if rest else None), None
    reg, p = _start(args)
    ledger = Ledger()
    try:
        if verb == "list":
            lines = handoff_lines(p, ledger)
            print("\n".join(lines) if lines else f"{p.name} has no handoffs.")
            return 0
        path = take_handoff(p, ledger, file, args.session)
    finally:
        ledger.close()
    refresh(reg, p)
    print(f"{p.name}: session {_one_line(args.session)} took {path.name}")
    return 0


@_run
def cmd_handoff(args) -> int:
    """`room handoff [project] --session <id> --reason <text> [--context <tokens>] [--file PATH|-]` (#363)."""
    if args.name in ("take", "list"):
        return _handoff_sub(args)
    if args.rest:
        raise HandshakeError(f"refused: unexpected {' '.join(args.rest)}. Name one project.")
    if not _one_line(args.session or "") or not _one_line(args.reason or ""):
        raise HandshakeError("refused: a handoff needs --session <id> and --reason <text>.")
    reg, p = _start(args)
    if args.file in (None, "-"):
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise HandshakeError(f"refused: can't read {args.file}: {exc}") from None
    ledger = Ledger()
    try:
        path = write_handoff(reg, p, ledger, text, args.session, args.reason, args.context)
    finally:
        ledger.close()
    refresh(reg, p)
    print(f"handoff for {p.name} from session {_one_line(args.session)}: {path}")
    return 0
