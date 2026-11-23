"""Roll the live checkout back to the last good sha when the first beat on new code fails (commonroom#453).

beat.cmd runs this only when `room.py beat` exits non-zero. Standard library only, and nothing from `commonroom`: a
broken package is exactly what this has to undo. It acts only when all hold:
- the beat failed (the exit code passed as the first argument is not 0),
- HEAD is the sha the room last moved the checkout to (`updated-to` in the room home),
- `last-good-sha` (the sha it moved from) exists and differs from it,
- the working tree is clean (`git status --porcelain` is empty).
Then `git checkout --quiet --detach <last-good-sha>`, `held-sha` = the bad sha, and one line in `rollback.log`. Never a
reset, never a stash. The dispatcher's update step then won't move to the held sha again (dispatch.py)."""
import os
import subprocess
import sys
import time
from pathlib import Path

LAST_GOOD, UPDATED_TO, HELD, LOG = "last-good-sha", "updated-to", "held-sha", "rollback.log"


def room_home() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom")


def _git(checkout: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(checkout).resolve().parent)}
    done = subprocess.run(["git", *args], cwd=checkout, capture_output=True, text=True, encoding="utf-8", env=env)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def rollback(checkout: Path, home: Path, beat_exit: int) -> str | None:
    """Returns the line written to rollback.log when it rolled back, else None."""
    if beat_exit == 0:
        return None
    good, bad = _read(home / LAST_GOOD), _read(home / UPDATED_TO)
    if not good or not bad or good == bad:
        return None
    if _git(checkout, "rev-parse", "HEAD").lower() != bad:
        return None
    if _git(checkout, "status", "--porcelain"):
        return None
    _git(checkout, "checkout", "--quiet", "--detach", good)
    (home / HELD).write_text(bad + "\n", encoding="utf-8")
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} beat exited {beat_exit} on {bad[:7]}: "
            f"checkout rolled back to {good[:7]}, {bad[:7]} held")
    with (home / LOG).open("a", encoding="utf-8") as log:
        log.write(line + "\n")
    return line


def main(argv: list[str]) -> int:
    try:
        beat_exit = int(argv[0]) if argv else 1
    except ValueError:
        beat_exit = 1
    try:
        line = rollback(Path(__file__).resolve().parent, room_home(), beat_exit)
    except Exception as exc:
        print(f"rollback FAILED: {exc}")
        return 1
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
