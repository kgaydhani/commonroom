"""A place's card: one screen any session can ask for (`room card <place or path>`, commonroom#572, docs/layer.md).

What the place is for, its state, where its method is written and its last few results, and who answers for it. Every
line ends with `[source]`, where it came from; a source with nothing to say is shown as `missing`, never guessed.
A project's card reads the registry, its `room.toml` and the ledger. A plain place's reads `places.json` and the session
records. Read-only: nothing is written, no model is called.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from commonroom import config, places, registry, sessions

METHOD_FOLDERS = ("ways", "docs/ways", "method")      # a folder here is the place's written method, listed by path
METHOD_FILES = ("METHOD.md", "docs/method.md", "docs/METHOD.md")
RESULTS = 3
TITLES = ("custom-title", "ai-title")
_TITLE = re.compile(rb'"type"\s*:\s*"(?:custom|ai)-title"')
SEND = ("A place with its own worked-out process is the signal to send the question there (`room note`, `room hand`, "
        "or `room route` once it exists), not to do the work blind.")


class CardError(Exception):
    """No place or project matches; the message says what was asked."""


def resolve(which: str, reg, data: dict):
    """(`project`, Project) or (`place`, record) for a name or a path (a path inside a project is that project)."""
    named = [p for p in reg.projects.values() if p.name.lower() == which.lower()]
    if len(named) == 1:
        return "project", named[0]
    if os.sep in which or "/" in which or ":" in which:
        key = sessions.path_key(which)
        held = sessions._within(key, [sessions.path_key(p.path) for p in reg.projects.values()])
        if held:
            return "project", next(p for p in reg.projects.values() if sessions.path_key(p.path) == held)
        if key in data["places"]:
            return "place", data["places"][key]
        if Path(which).is_dir():
            return "place", {"path": str(Path(which)), "last": 0.0, "sessions": [], "description": "", "unseen": True}
        raise CardError(f"no place or project at {which!r}")
    hit = [p for p in data["places"].values() if Path(p["path"]).name.lower() == which.lower()]
    if len(hit) == 1:
        return "place", hit[0]
    raise CardError(f"no place or project {which!r}" if not hit else f"{which!r} names {len(hit)} places: give the path")


def _day(at) -> str:
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") if at else "never"


def _method(root: Path) -> list[str]:
    found = [f"{rel}/ (folder)" for rel in METHOD_FOLDERS if (root / rel).is_dir()]
    found += [rel for rel in METHOD_FILES if (root / rel).is_file()]
    return [f"method: {m} [on disk: {root / m.split(' ')[0]}]" for m in found] or ["method: missing [no ways/ folder or method notes found]"]


def _titles(ids, folder: Path) -> dict:
    """Session id -> its latest title, read from the session's own transcript; only title lines are parsed."""
    out: dict = {}
    for sid in ids:
        for path in folder.glob(f"*/{sid}.jsonl"):
            try:
                with open(path, "rb") as handle:
                    for raw in handle:
                        if _TITLE.search(raw):
                            try:
                                line = json.loads(raw)
                                title = line.get("customTitle") or line.get("aiTitle")
                            except ValueError:
                                continue
                            if isinstance(title, str) and title.strip():
                                out[sid] = " ".join(title.split())
            except OSError:
                continue
    return out


def _sessions_line(path: str, projects_dir: Path | None, now: float) -> str:
    folder = projects_dir or sessions.default_dir()
    spans = sessions.spans({"here": path}, 0.0, now, projects_dir=folder)
    weight: dict = {}
    for s in spans:
        if s["project"] == "here":
            weight[s["session_id"]] = weight.get(s["session_id"], 0) + s["messages"]
    top = sorted(weight, key=lambda k: (-weight[k], k))[:3]
    if not top:
        return "answers: missing [no session records in this folder]"
    titles = _titles(top, folder)
    return "answers: sessions with most of the work: " + "; ".join(
        f"{titles.get(sid, '(untitled)')} ({sid}, {weight[sid]} messages)" for sid in top) + " [session records]"


def build(which: str, reg, data: dict, ledger=None, projects_dir: Path | None = None, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    kind, item = resolve(which, reg, data)
    if kind == "place":
        path = item["path"]
        exists = Path(path).is_dir()
        out = [f"{Path(path).name or path}: a place (no room.toml, no share) at {path} [places.json]",
               "for: " + (f"{item['description']} [places.json]" if item.get("description")
                          else "missing [no description set: `room places describe <place> \"line\"`]"),
               f"state: last used {_day(item['last'])}, {len(item['sessions'])} sessions seen; "
               f"{'on disk' if exists else 'GONE from disk'} [places.json, disk]"
               if not item.get("unseen") else "state: not in places.json (no session record names it); "
               f"{'on disk' if exists else 'GONE from disk'} [disk]"]
        out += _method(Path(path)) if exists else ["method: missing [folder is gone]"]
        out.append("results: missing [a place has no ledger runs]")
        out.append(_sessions_line(path, projects_dir, now))
    else:
        root = Path(item.path)
        try:
            cfg = config.load(root / "room.toml", follow_host=False)
        except (OSError, ValueError):
            cfg = {}
        mission = (cfg.get("project") or {}).get("mission")
        out = [f"{item.name}: a project at {item.path} (share {item.share:g}%) [registry]",
               "for: " + (f"{mission} [room.toml mission]" if mission else "missing [room.toml has no mission]")]
        if ledger is None:
            out.append("state: missing [no ledger]")
            runs = []
        else:
            runs = ledger.recent_runs(item.name, limit=200, finished=True)
            land = next((r for r in runs if any(o.get("kind") == "merge" for o in r["outputs"])), None)
            breakers = ledger.open_breakers(item.name)
            failing = [c for c, codes in ledger.heartbeat_runs(item.name).items() if codes and codes[0] != 0]
            out.append("state: last landing " + (f"{_day(land['ended_at'])} ({land['task']}) [ledger runs]" if land
                                                 else "missing [no merge in the ledger]"))
            out.append("  open breakers: " + ("; ".join(f"{b['kind']}: {b['detail']}" for b in breakers) or "none")
                       + " [ledger breakers]")
            out.append("  failing heartbeat commands: " + ("; ".join(failing) or "none") + " [ledger heartbeat_runs]")
        out += _method(root)
        for r in runs[:RESULTS]:
            out.append(f"result: {_day(r['ended_at'])} {r['role']} {r['task']}: {r['status']} [ledger runs]")
        if not runs:
            out.append("results: missing [no finished runs in the ledger]")
        director = (cfg.get("roles") or {}).get("director")
        out.append("answers: its director (" + ", ".join(director) + ") [room.toml roles]" if director
                   else _sessions_line(item.path, projects_dir, now))
    out.append(SEND)
    return out
