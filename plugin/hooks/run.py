"""Launcher for the plugin's hooks (commonroom#565): `python run.py <event>` runs `room.py hook <event>`.

It finds the room's code in this order: the checkout the plugin sits in (plugin/hooks/run.py -> ../../room.py), then
the path in the file `room-path` in the room home (COMMONROOM_HOME, or ~/.commonroom), which an install writes. That
makes a plugin copied out of the checkout (a user-scope install) work. Found nothing: exit 0 and print nothing.
Standard library only; it must not import room code before it has found it."""
import os
import runpy
import sys
from pathlib import Path


def find_room() -> Path | None:
    here = Path(__file__).resolve().parents[2] / "room.py"
    if here.is_file():
        return here
    try:
        home = Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom")
        text = (home / "room-path").read_text(encoding="utf-8").strip()
        found = Path(text)
        return found if text and found.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def main(argv: list[str]) -> int:
    room = find_room()
    if room is None or not argv:
        return 0
    sys.argv = [str(room), "hook", *argv]
    runpy.run_path(str(room), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
