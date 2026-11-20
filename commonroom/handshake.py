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
import sys
import tempfile
import time
import tomllib
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from commonroom import config as config_mod, ledger as ledger_mod, metrics, registry
from commonroom.dispatch import fill_way, read_way
from commonroom.ledger import Ledger

TEMPLATES = Path(__file__).resolve().parent / "ways" / "handshake"
README, CHARTER, DIGEST = (read_way(TEMPLATES / f"{name}.md") for name in ("README", "charter", "digest"))
WEEK = 7 * 86400
NEWEST = 5                 # notes shown in the digest; `room notes` shows them all
NOTE_CHARS = 800           # of each note's text in the digest
ATTACHED = "attached:"     # ledger mark key prefix: attached:<who>
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


def find(reg: registry.Registry, name: str | None = None, cwd: Path | None = None) -> registry.Project:
    """The project named, or else the one whose folder (or room folder) holds `cwd`, the deepest if several do."""
    names = ", ".join(sorted(reg.projects))
    if name:
        match = reg.projects.get(name) or next((p for n, p in reg.projects.items() if n.lower() == name.lower()), None)
        if match is None:
            raise HandshakeError(f"no project named {name!r} is registered (registered: {names}).")
        return match
    cwd = Path.cwd() if cwd is None else cwd
    found = [(len(os.path.abspath(where)), p) for p in reg.projects.values()
             for where in (p.path, folder_of(p)) if isinstance(where, (str, Path)) and str(where) and _inside(cwd, where)]
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
    """Make the folder and its parts where missing. The README follows its template; the charter is only ever created."""
    where = folder_of(p)
    (where / "inbox").mkdir(parents=True, exist_ok=True)
    readme = fill_way(README, project=p.name) + "\n"
    try:
        same = (where / "README.md").read_text(encoding="utf-8") == readme
    except (OSError, UnicodeDecodeError):
        same = False
    if not same:
        _write(where / "README.md", readme)
    try:
        with open(where / "charter.md", "x", encoding="utf-8", newline="\n") as handle:
            handle.write(fill_way(CHARTER, project=p.name) + "\n")
    except FileExistsError:
        pass
    return where


def _one_line(value: str) -> str:
    return " ".join(str(value).split())


def write_note(where: Path, text: str, sender: str, to: str | None = None, now: float | None = None) -> Path:
    """One new file in `inbox/`, never an existing one: a name taken (two notes in one instant) moves on a microsecond,
    so names still sort by time."""
    stamp = datetime.fromtimestamp(time.time() if now is None else now)
    head = ["---", f"from: {_one_line(sender)}", *([f"to: {_one_line(to)}"] if to else []),
            f"at: {stamp:%Y-%m-%dT%H:%M:%S}", "---", ""]
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
        note = {"name": path.name, "at": path.stat().st_mtime, "from": None, "to": None, "text": None, "dated": False}
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
    who = (f", from {n['from']}" if n["from"] else "") + (f" to {n['to']}" if n["to"] else "")
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


def _charter(where: Path, p: registry.Project) -> str:
    try:
        text = (where / "charter.md").read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except FileNotFoundError:
        return "There is no `charter.md` in this folder."
    def body(t): return "\n".join(t.strip().split("\n")[1:] if t.lstrip().startswith("# ") else t.strip().split("\n")).strip()
    if body(text) == body(fill_way(CHARTER, project=p.name)):
        cfg = room_toml(p)
        mission, goal = (cfg.get("project") or {}).get("mission"), (cfg.get("goal") or {}).get("milestone")
        lines = ["Not written yet: `charter.md` holds only its headings."]
        if mission:
            lines += ["Until it is, here is the mission from the project's room.toml:", "",
                      "\n".join(f"> {line}".rstrip() for line in str(mission).strip().split("\n"))]
        if goal:
            lines += ["", f"Its current goal is the milestone \"{goal}\"."]
        return "\n".join(lines)
    return re.sub(r"(?m)^(#+ )", r"#\1", body(text)) or "Not written yet: `charter.md` is empty."


def _share(reg: registry.Registry, p: registry.Project) -> str:
    share = registry.fmt_number(p.share) if registry.is_number(p.share) else repr(p.share)
    mode = "; ".join(f"`{m}`: {MODES[m]}" if m in MODES else f"`{m}`" for m in (p.work, p.when))
    strict = " Its unused share is never lent to other projects." if p.strict is True else ""
    return (f"{share}% of the owner's weekly allowance ({mode}).{strict} The owner's own share "
            f"is {registry.fmt_number(reg.owner_share())}%. Set in `{registry.default_path()}`.")


def _attached(ledger: Ledger, p: registry.Project) -> str:
    marks = ledger.marks(p.name, ATTACHED)
    return "\n".join(f"- {who}, last {_when(at)}" for who, at in marks) or "Nobody has run `room attach` here yet."


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
    for r in sorted(landed, key=lambda r: r["started_at"], reverse=True):
        refs = ", ".join(o.get("ref") for o in r["outputs"] if o.get("ref"))
        lines.append(f"- {_when(r['started_at'])}: {r['task'] or 'no item'}{f' ({refs})' if refs else ''}")
    return "\n".join(lines)


def build_digest(reg: registry.Registry, p: registry.Project, ledger: Ledger, now: float | None = None) -> str:
    now = time.time() if now is None else now
    where = folder_of(p)
    return fill_way(DIGEST, project=p.name, built=f"{datetime.fromtimestamp(now):%a %d %b %Y %H:%M} (local time)",
                    charter=_charter(where, p), share=_share(reg, p), attached=_attached(ledger, p),
                    notes=_notes(where), work=_work(ledger, p, now)) + "\n"


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


def _user() -> str:
    try:
        return getpass.getuser() or "someone"
    except (OSError, KeyError, ImportError):
        return "someone"


def _start(args) -> tuple[registry.Registry, registry.Project]:
    reg = load_registry()
    return reg, find(reg, getattr(args, "project", None))


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
    who = _one_line(args.who or _user())
    text = refresh(reg, p, who=who)
    where = folder_of(p)
    names = sorted((e.name for e in where.iterdir() if not e.name.startswith(".")),
                   key=lambda n: (PARTS.index(n) if n in PARTS else len(PARTS), n.lower()))
    held = [n + ("/" if (where / n).is_dir() else "") for n in names]
    print(f"{p.name}: {where}\nHolds {', '.join(held)} ({len(read_notes(where))} notes in inbox/).")
    print(f"Attached as {who}.\n\n{text}", end="")
    return 0


@_run
def cmd_note(args) -> int:
    text = sys.stdin.read() if args.text == "-" else args.text
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
    path = write_note(prepare(p), text, sender, args.to)
    refresh(reg, p)
    print(f"note for {p.name} from {_one_line(sender)}: {path}")
    return 0


@_run
def cmd_notes(args) -> int:
    reg, p = _start(args)
    notes = read_notes(folder_of(p))
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
def cmd_digest(args) -> int:
    reg, p = _start(args)
    print(refresh(reg, p), end="")
    return 0
