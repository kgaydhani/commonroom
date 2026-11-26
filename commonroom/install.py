"""`room install` / `room uninstall` for Claude Code at user level (commonroom#566).

Install adds three things to the user's settings file (CLAUDE_CONFIG_DIR or ~/.claude, `settings.json`): the status
line (only if none is set), this checkout as a plugin marketplace, and the plugin enabled. It also writes `room-path`
in the room home, which the plugin's launcher reads. It prints each change first; `--dry-run` writes nothing. Install
records the file's original bytes and what it wrote in `install.json` in the room home; uninstall puts the original
bytes back when the file is still as install left it, and otherwise removes only the keys install added.
The owner's limits.txt ships with no active rules; install merges its lines into permissions.deny and tracks
individual rules, so removing limits or uninstalling preserves deny rules the owner already had."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from commonroom import limits, person, wire

MARKETPLACE = "commonroom"
PLUGIN_KEY = "commonroom@commonroom"
MANIFEST = "install.json"
PATH_KEY = "room-path"  # manifest entry for the room-path file
CODEX_KEY, BEAT_KEY = "codex-hooks", "beat"
LF, CRLF = chr(10), chr(13) + chr(10)
PERSON_KEY = "person-import"  # the lines added to each vendor's user-level instruction file; every other key is a settings file


def room_home() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom")


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def checkout() -> Path:
    return Path(__file__).resolve().parent.parent


def person_targets() -> list[tuple[Path, str]]:
    """(instruction file, the one line install adds) per vendor. Claude Code imports a file with `@path`; Codex has no
    import syntax, so its line asks the session to read the file. Neither adds text per prompt."""
    where = person.path(room_home()).as_posix()
    return [(claude_dir() / "CLAUDE.md", f"@{where}"),
            (wire.codex_dir() / "AGENTS.md", f"Read the owner's person file at {where} at the start of every session.")]


def _person_plan() -> list[dict]:
    """The lines still to add: {file, line, text (the file's new text), existed, nl (a newline was added first)}."""
    out = []
    for f, line in person_targets():
        raw = f.read_bytes().decode("utf-8") if f.exists() else None
        if raw is not None and line in [x.strip() for x in raw.splitlines()]:
            continue
        nl = CRLF if raw and CRLF in raw else LF
        lead = nl if raw and not raw.endswith(LF) else ""
        out.append({"file": f, "line": line, "existed": raw is not None, "nl": bool(lead),
                    "text": (raw or "") + lead + line + nl})
    return out


def _person_remove(rec: dict) -> None:
    """Take out the one line install added (and the newline it added before it); the rest is left as it is."""
    f = Path(rec["file"])
    if not f.exists():
        return
    parts = f.read_bytes().decode("utf-8").splitlines(keepends=True)
    idx = [i for i, x in enumerate(parts) if x.strip() == rec["line"]]
    if not idx:
        return
    gone = parts.pop(idx[-1])
    if rec["nl"] and idx[-1] == len(parts) and parts:
        parts[-1] = parts[-1].rstrip(CRLF)
    if not rec["existed"] and not "".join(parts).strip():
        f.unlink()
    else:
        f.write_bytes("".join(parts).encode("utf-8"))


def _wanted(root: Path) -> list[tuple[list[str], object]]:
    """(key path, value) for each setting install adds."""
    posix = root.as_posix()
    return [
        (["statusLine"], {"type": "command", "command": f"python {posix}/room.py tap"}),
        (["extraKnownMarketplaces", MARKETPLACE], {"source": {"source": "directory", "path": posix}}),
        (["enabledPlugins", PLUGIN_KEY], True),
    ]


def _get(d: dict, path: list[str]):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None, False
        d = d[k]
    return d, True


def _set(d: dict, path: list[str], value) -> None:
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value


def _indent(text: str) -> int | str:
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            lead = line[: len(line) - len(stripped)]
            return "\t" if lead[0] == "\t" else len(lead)
    return 2


def _render(data: dict, original: str | None) -> bytes:
    text = json.dumps(data, indent=_indent(original or ""), ensure_ascii=False)
    nl = "\r\n" if original and "\r\n" in original else "\n"
    text = text.replace("\n", nl)
    if original is None or original.endswith("\n"):
        text += nl
    return text.encode("utf-8")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def plan(settings: Path, root: Path) -> tuple[dict, list[str], list[str], bytes | None]:
    """(new settings, key paths to add, lines for what is skipped, the file's current bytes or None)."""
    raw = settings.read_bytes() if settings.exists() else None
    data = json.loads(raw.decode("utf-8")) if raw and raw.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{settings} is not a JSON object")
    added, skipped = [], []
    for path, value in _wanted(root):
        have, present = _get(data, path)
        name = ".".join(path)
        if present and have == value:
            continue
        if present:
            skipped.append(f"{name} is already set to something else; left alone")
            continue
        _set(data, path, value)
        added.append(name)
    return data, added, skipped, raw


def cmd_install(args) -> int:
    root, home = checkout(), room_home()
    settings = claude_dir() / "settings.json"
    manifest = home / MANIFEST
    limits_file = home / limits.FILENAME
    limits_new = not limits_file.exists()
    try:
        old = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        data, added, skipped, raw = plan(settings, root)
        rules = limits.load(limits_file)
        deny_added, deny_removed = limits.plan(data, rules, old.get(str(settings), {}).get("deny_added", []))
    except (ValueError, OSError) as e:
        print(f"refused: {e}")
        return 2
    path_file = home / "room-path"
    room_py = (root / "room.py").as_posix()
    path_change = not path_file.exists() or path_file.read_text(encoding="utf-8").strip() != room_py
    codex_file = wire.codex_dir() / "hooks.json"
    try:
        codex_data, codex_added, codex_new = wire.codex_plan(codex_file, root)
    except (ValueError, OSError) as e:
        print(f"refused: {e}")
        return 2
    beat_done = wire.beat_registered(root)
    beat_cmd, _ = wire.beat_command(root)
    old_beats = [] if beat_done else wire.old_beats(root)
    person_add = _person_plan()
    print(f"Settings file: {settings}")
    for name in added:
        print(f"  add    {name}")
    for line in skipped:
        print(f"  skip   {line}")
    print(f"Limits file: {limits_file}" + (" (create with commented examples; no active rules)" if limits_new else ""))
    for rule in rules:
        action = "add" if rule in deny_added else "keep"
        print(f"  {action:6} permissions.deny: {rule} — a deny rule cannot be lifted from inside a session")
    for rule in deny_removed:
        print(f"  remove permissions.deny: {rule}")
    if path_change:
        print(f"Room home: {path_file}\n  write  {room_py}")
    print(f"Codex hooks file: {codex_file}")
    for event in codex_added:
        print(f"  add    hooks.{event}")
    for rec in person_add:
        print(f"Instruction file: {rec['file']}{LF}  add    {rec['line']}")
    if old_beats:
        print("Beat: not registered, these enabled tasks already run this checkout's heartbeat: "
              + ", ".join(f'"{n}"' for n in old_beats))
        print("  To switch to the beat, retire each, then run `room install` again:")
        for n in old_beats:
            print(f'    schtasks /delete /tn "{n}" /f')
    else:
        print(f"Beat: {'already registered' if beat_done else 'register ' + beat_cmd}")
    settings_change = bool(added or deny_added or deny_removed)
    if not settings_change and not limits_new and not path_change and not codex_added and not person_add and (beat_done or old_beats):
        print("Nothing to change: already installed.")
        return 0
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    if path_change and PATH_KEY not in old:
        # Only the first install records what was there before, so a second one can't mistake our file for the user's.
        prev = path_file.read_text(encoding="utf-8") if path_file.exists() else None
        old[PATH_KEY] = {"previous": prev}
    if settings_change:
        before = json.loads(raw.decode("utf-8")) if raw and raw.strip() else {}
        made = [p[0] for p, _ in _wanted(root) if len(p) > 1 and ".".join(p) in added and p[0] not in before]
        if deny_added and "permissions" not in before:
            made.append("permissions")
        new_bytes = _render(data, raw.decode("utf-8") if raw is not None else None)
        entry = old.get(str(settings), {})
        if entry and (raw is None or _sha(raw) != entry["written_sha"]):
            # The file changed since the last install, so the saved original no longer describes it.
            entry["edited"] = True
        entry.setdefault("existed", raw is not None)
        entry.setdefault("original", base64.b64encode(raw or b"").decode())
        entry["added"] = sorted(set(entry.get("added", [])) | set(added))
        entry["created"] = sorted(set(entry.get("created", [])) | set(made))
        entry["deny_added"] = list(dict.fromkeys([r for r in entry.get("deny_added", []) if r in rules] + deny_added))
        entry["deny_created"] = entry.get("deny_created", False) or (bool(deny_added) and "deny" not in before.get("permissions", {}))
        entry["written_sha"] = _sha(new_bytes)
        old[str(settings)] = entry
    if codex_added:
        old.setdefault(CODEX_KEY, {"file": str(codex_file), "created": codex_new})
    if person_add:
        old[PERSON_KEY] = old.get(PERSON_KEY, []) + [{k: str(v) if k == "file" else v for k, v in r.items() if k != "text"}
                                                      for r in person_add]
    ok, why = beat_done, ""
    if not beat_done and not old_beats:
        ok, why = wire.beat_register(root)
        if ok:
            old[BEAT_KEY] = {"command": beat_cmd}
    if settings_change or limits_new or path_change or codex_added or person_add or BEAT_KEY in old:
        home.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
    if settings_change:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_bytes(new_bytes)
    if limits_new:
        limits_file.write_bytes(limits.TEMPLATE.read_bytes())
    if path_change:
        home.mkdir(parents=True, exist_ok=True)
        path_file.write_text(room_py + "\n", encoding="utf-8")
    if codex_added:
        wire.codex_write(codex_file, codex_data)
    for rec in person_add:
        rec["file"].parent.mkdir(parents=True, exist_ok=True)
        rec["file"].write_bytes(rec["text"].encode("utf-8"))
    if not ok:
        print(f"Could not register the beat here ({why or 'no output'}). Run this yourself:\n{beat_cmd}")
    if codex_added:
        print("Codex asks you to trust the new hooks once: open /hooks in Codex.")
    print("Installed. Open a new Claude Code session to load the plugin.")
    return 0


def _remove(data: dict, name: str, root: Path, created: list[str]) -> bool:
    path = name.split(".")
    wanted = dict((".".join(p), v) for p, v in _wanted(root))
    have, present = _get(data, path)
    if not present or have != wanted.get(name):
        return False
    parent, _ = _get(data, path[:-1]) if len(path) > 1 else (data, True)
    del parent[path[-1]]
    if len(path) > 1 and not parent and path[0] in created:
        _remove_empty(data, path[:-1])
    return True


def _remove_empty(data: dict, path: list[str]) -> None:
    parent, _ = _get(data, path[:-1]) if len(path) > 1 else (data, True)
    del parent[path[-1]]


def cmd_uninstall(args) -> int:
    home = room_home()
    manifest = home / MANIFEST
    path_file = home / "room-path"
    if not manifest.exists():
        print("Nothing to uninstall: the room is not installed.")
        return 0
    state = json.loads(manifest.read_text(encoding="utf-8"))
    path_rec = state.pop(PATH_KEY, None)
    codex_rec = state.pop(CODEX_KEY, None)
    beat_rec = state.pop(BEAT_KEY, None)
    person_recs = state.pop(PERSON_KEY, [])
    root = checkout()
    actions = []
    for file, entry in state.items():
        f = Path(file)
        current = f.read_bytes() if f.exists() else None
        if current is not None and not entry.get("edited") and _sha(current) == entry["written_sha"]:
            actions.append((f, entry, "restore"))
        else:
            actions.append((f, entry, "remove"))
    for f, entry, how in actions:
        print(f"Settings file: {f}")
        if how == "restore":
            print("  restore the file as it was before install" if entry["existed"] else "  delete the file install created")
        else:
            for name in entry["added"]:
                print(f"  remove {name}")
            for rule in entry.get("deny_added", []):
                print(f"  remove permissions.deny: {rule}")
    if codex_rec is not None:
        print(f"Codex hooks file: {codex_rec['file']}\n  remove our SessionStart and UserPromptSubmit entries")
    for rec in person_recs:
        print(f"Instruction file: {rec['file']}{LF}  remove {rec['line']}")
    if beat_rec is not None:
        print(f"Beat: remove the scheduled {beat_rec['command'].split()[0]} entry")
    if path_rec is not None:
        print(f"Room home: {'delete' if path_rec.get('previous') is None else 'restore'} {path_file}")
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    for f, entry, how in actions:
        if how == "restore":
            if entry["existed"]:
                f.write_bytes(base64.b64decode(entry["original"]))
            else:
                f.unlink()
        elif f.exists():
            raw = f.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            changed = limits.remove(data, entry)
            if any([_remove(data, n, root, entry.get("created", [])) for n in entry["added"]]) or changed:
                f.write_bytes(_render(data, raw.decode("utf-8")))
    if codex_rec is not None:
        wire.codex_remove(Path(codex_rec["file"]), root, codex_rec["created"])
    for rec in person_recs:
        _person_remove(rec)
    if beat_rec is not None and not wire.beat_unregister(root):
        print(f"Could not remove the beat; remove it yourself (see: room schedule).")
    if path_rec is not None:
        prev = path_rec.get("previous")
        if prev is None:
            path_file.unlink(missing_ok=True)
        else:
            path_file.write_text(prev, encoding="utf-8")
    manifest.unlink(missing_ok=True)
    print("Uninstalled.")
    return 0
