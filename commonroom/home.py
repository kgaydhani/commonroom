"""The home place (commonroom#579, docs/layer.md): one folder in the room home that is no project, where the owner
opens a session to talk about anything. Its instruction file gives the map (places.json) and the person file by pointer
and says its job is to route and start work elsewhere, never to do project work. `room home` makes it once; it is
recorded in places.json with `"home": true`, so `room places` shows it and the session hook knows it. It is never
registered as a project: it has no share, dispatcher or breaker."""
from __future__ import annotations

from pathlib import Path

from commonroom import ledger as ledger_mod, person, places, sessions

FOLDER = "home"
INSTRUCTIONS = "CLAUDE.md"
TEXT = """# Common Room home

This folder is the owner's home place: where he opens a session to talk about anything. It is no project.

Your job here is to route and start work elsewhere, never to do project work yourself. Find the right place or
project, then start the work there (`room route`, `room hand`, or open a session in that folder).

- The map of his folders: `{map}` (`room places` lists it; `room card <place>` shows one).
- The person file, about him: `{person}`. Read it at the start.

Keep this session light. Replace it rather than let it grow long.
"""


def folder(home: Path | None = None) -> Path:
    return (home if home is not None else ledger_mod.default_path().parent) / FOLDER


def create(home: Path | None = None) -> tuple[Path, bool]:
    """Make the home folder, its instruction file and its place record. Returns (folder, created now). Idempotent:
    an existing instruction file is never rewritten, and the place is recorded once."""
    where = folder(home)
    made = not (where / INSTRUCTIONS).exists()
    where.mkdir(parents=True, exist_ok=True)
    if made:
        text = TEXT.format(map=places.path_file().as_posix(), person=person.path(home).as_posix())
        (where / INSTRUCTIONS).write_text(text, encoding="utf-8", newline="\n")
    data = places.load()
    key = sessions.path_key(where)
    record = data["places"].get(key)
    if record is None or not record.get("home"):
        data["places"][key] = {"path": str(where), "last": (record or {}).get("last", 0.0),
                               "sessions": (record or {}).get("sessions", []),
                               "description": (record or {}).get("description") or "the home place: routes and starts work elsewhere",
                               "home": True}
        places.save(data)
    return where, made
