"""`room install` / `room uninstall` for Claude Code at user level (commonroom#566).

Install adds three things to the user's settings file (CLAUDE_CONFIG_DIR or ~/.claude, `settings.json`): the status
line (only if none is set), this checkout as a plugin marketplace, and the plugin enabled. It also writes `room-path`
in the room home, which the plugin's launcher reads. It prints each change first; `--dry-run` writes nothing. Install
records the file's original bytes and what it wrote in `install.json` in the room home; uninstall puts the original
bytes back when the file is still as install left it, and otherwise removes only the keys install added."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

MARKETPLACE = "commonroom"
PLUGIN_KEY = "commonroom@commonroom"
MANIFEST = "install.json"


def room_home() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom")


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def checkout() -> Path:
    return Path(__file__).resolve().parent.parent


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
    try:
        data, added, skipped, raw = plan(settings, root)
    except (ValueError, OSError) as e:
        print(f"refused: {e}")
        return 2
    path_file = home / "room-path"
    room_py = (root / "room.py").as_posix()
    path_change = not path_file.exists() or path_file.read_text(encoding="utf-8").strip() != room_py
    print(f"Settings file: {settings}")
    for name in added:
        print(f"  add    {name}")
    for line in skipped:
        print(f"  skip   {line}")
    if path_change:
        print(f"Room home: {path_file}\n  write  {room_py}")
    if not added and not path_change:
        print("Nothing to change: already installed.")
        return 0
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    manifest = home / MANIFEST
    if added:
        old = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        new_bytes = _render(data, raw.decode("utf-8") if raw is not None else None)
        entry = old.get(str(settings), {})
        entry.setdefault("existed", raw is not None)
        entry.setdefault("original", base64.b64encode(raw or b"").decode())
        entry["added"] = sorted(set(entry.get("added", [])) | set(added))
        entry["written_sha"] = _sha(new_bytes)
        old[str(settings)] = entry
        home.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_bytes(new_bytes)
    if path_change:
        home.mkdir(parents=True, exist_ok=True)
        path_file.write_text(room_py + "\n", encoding="utf-8")
    print("Installed. Open a new Claude Code session to load the plugin.")
    return 0


def _remove(data: dict, name: str, root: Path) -> bool:
    path = name.split(".")
    wanted = dict((".".join(p), v) for p, v in _wanted(root))
    have, present = _get(data, path)
    if not present or have != wanted.get(name):
        return False
    parent, _ = _get(data, path[:-1]) if len(path) > 1 else (data, True)
    del parent[path[-1]]
    if len(path) > 1 and not parent:
        _remove_empty(data, path[:-1])
    return True


def _remove_empty(data: dict, path: list[str]) -> None:
    parent, _ = _get(data, path[:-1]) if len(path) > 1 else (data, True)
    del parent[path[-1]]


def cmd_uninstall(args) -> int:
    home = room_home()
    manifest = home / MANIFEST
    path_file = home / "room-path"
    if not manifest.exists() and not path_file.exists():
        print("Nothing to uninstall: the room is not installed.")
        return 0
    state = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    root = checkout()
    actions = []
    for file, entry in state.items():
        f = Path(file)
        current = f.read_bytes() if f.exists() else None
        if current is not None and _sha(current) == entry["written_sha"]:
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
    if path_file.exists():
        print(f"Room home: delete {path_file}")
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
            if any([_remove(data, n, root) for n in entry["added"]]):
                f.write_bytes(_render(data, raw.decode("utf-8")))
    path_file.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    print("Uninstalled.")
    return 0
