"""Places: every folder the owner has worked in that is not a project (commonroom#570, docs/layer.md).

A place is a folder with sessions and neither a `room.toml` nor a share: name, path, last use, session count, whether it
is still on disk, and a one-line description the owner sets (never guessed). Places are found from the session records
`commonroom.sessions` reads (`~/.claude/projects/`) and kept in `<room home>/places.json`, so they outlive the records
being pruned. Discovery only reads those records; it never writes into, moves or renames any folder.

Left out: a registered project's folder and anything inside it, any folder holding a `room.toml`, worker worktrees
(each registered project's worktrees folder, its `<project>-wt` sibling and its `.claude/worktrees`) and temp folders.
A place is only a record here: it has no dispatcher, no share and no breaker, and nothing in the registry or the pie
reads this file.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

from commonroom import registry, sessions

FILENAME = "places.json"
_CWD = re.compile(rb'"cwd"\s*:')


def path_file() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom") / FILENAME


def load(path: Path | None = None) -> dict:
    """{"places": {path key: {...}}, "files": {transcript key: [size, mtime]}}. Missing or corrupt: empty."""
    try:
        data = json.loads((path or path_file()).read_text(encoding="utf-8"))
        if isinstance(data["places"], dict) and isinstance(data["files"], dict):
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {"places": {}, "files": {}}


def save(data: dict, path: Path | None = None) -> None:
    path = path or path_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
        json.dump(data, out, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _worktree_roots(reg) -> list[str]:
    """Path keys of every registered project's worker worktree folders."""
    roots = []
    for project in reg.projects.values():
        root = Path(project.path)
        roots += [root.parent / f"{root.name}-wt", root / ".claude" / "worktrees"]
        try:
            with open(root / "room.toml", "rb") as handle:
                section = tomllib.load(handle).get("project") or {}
                configured, previous = section.get("worktrees"), section.get("worktrees_previous")
        except (OSError, tomllib.TOMLDecodeError):
            configured, previous = None, None
        if isinstance(configured, str) and configured:
            roots.append(Path(configured))
        if isinstance(previous, str):
            previous = [previous]
        if isinstance(previous, list):
            roots += [Path(p) for p in previous if isinstance(p, str) and p]
    return [sessions.path_key(r) for r in roots]


def _excluded(cwd: str, project_roots, worktree_roots, temp) -> bool:
    key = sessions.path_key(cwd)
    if sessions._within(key, project_roots) or sessions._within(key, worktree_roots) or sessions._within(key, [temp]):
        return True
    return (Path(cwd) / "room.toml").is_file()


def _read(path: Path):
    """(cwd, session id, time) of each line of a transcript that has them; nothing else is read or kept."""
    try:
        handle = open(path, "rb")
    except OSError:
        return
    seen = set()
    with handle:
        for raw in handle:
            if not _CWD.search(raw):
                continue
            try:
                line = json.loads(raw)
                cwd, session = line["cwd"], line["sessionId"]
                at = datetime.fromisoformat(line["timestamp"]).timestamp()
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
            if isinstance(cwd, str) and isinstance(session, str) and cwd and session and (cwd, session, at) not in seen:
                seen.add((cwd, session, at))
                yield cwd, session, at


def discover(reg=None, projects_dir: Path | None = None, path: Path | None = None) -> dict:
    """Read the session records not read before, add what they show to the saved places and save. Returns the data."""
    reg = reg if reg is not None else registry.load(check=False)
    data = load(path)
    project_roots = [sessions.path_key(p.path) for p in reg.projects.values()]
    worktrees, temp = _worktree_roots(reg), sessions.path_key(tempfile.gettempdir())
    found: dict = {}
    files: dict = {}
    folder = projects_dir or sessions.default_dir()
    try:
        transcripts = sorted(folder.glob("*/*.jsonl")) + sorted(folder.glob("*/*/subagents/*.jsonl"))
    except OSError:
        transcripts = []
    for transcript in transcripts:
        key = sessions.path_key(transcript)
        try:
            st = transcript.stat()
        except OSError:
            continue
        files[key] = [st.st_size, st.st_mtime]
        if data["files"].get(key) == files[key]:
            continue
        for cwd, session, at in _read(transcript):
            place = found.setdefault(sessions.path_key(cwd), {"path": cwd, "last": 0.0, "sessions": set()})
            place["sessions"].add(session)
            if at >= place["last"]:
                place["last"], place["path"] = at, cwd
    places = data["places"]
    for key, new in found.items():
        if _excluded(new["path"], project_roots, worktrees, temp):
            continue
        old = places.get(key) or {"description": "", "sessions": []}
        places[key] = {"path": new["path"] if new["last"] >= old.get("last", 0) else old["path"],
                       "last": max(new["last"], old.get("last", 0)),
                       "sessions": sorted(set(old["sessions"]) | new["sessions"]),
                       "description": old.get("description", "")} | ({"home": True} if old.get("home") else {})
    for key in [k for k, p in places.items() if _excluded(p["path"], project_roots, worktrees, temp)]:
        del places[key]                       # a folder that became a project (or holds a room.toml) is no place
    data["files"] = {k: v for k, v in files.items()} | {k: v for k, v in data["files"].items() if k not in files}
    save(data, path)
    return data


def listing(data: dict) -> list[dict]:
    """Each place as {"name", "path", "last", "sessions", "exists", "description"}, most recent first."""
    rows = [{"name": Path(p["path"]).name or p["path"], "path": p["path"], "last": p["last"],
             "sessions": len(p["sessions"]), "exists": Path(p["path"]).is_dir(), "description": p.get("description", ""),
             "home": bool(p.get("home"))}
            for p in data["places"].values()]
    return sorted(rows, key=lambda r: (-r["last"], r["path"]))


def describe(data: dict, which: str, line: str) -> dict:
    """Set the description of the place named or pathed `which`. Raises KeyError when none or several match."""
    key = sessions.path_key(which) if os.sep in which or "/" in which else None
    matches = [k for k, p in data["places"].items()
               if k == key or (key is None and Path(p["path"]).name.lower() == which.lower())]
    if len(matches) != 1:
        raise KeyError(f"no place {which!r}" if not matches else f"{which!r} names {len(matches)} places: give the path")
    data["places"][matches[0]]["description"] = " ".join(line.split())
    return data


def find(data: dict, cwd) -> dict | None:
    """The saved place that `cwd` is, or is inside, as its record; None when none. Reads only `data`."""
    key = sessions.path_key(cwd)
    root = sessions._within(key, list(data["places"]))
    return data["places"][root] if root else None
