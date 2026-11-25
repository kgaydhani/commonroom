"""Publish the exported page after a beat (commonroom#649).

`<room home>/page.toml` holds the owner's own settings; nothing about them is in this repo:

    [page]
    folder = "C:/path/to/public"      # optional; default <room home>/page/public
    publish = 'some-tool deploy "{folder}"'   # absent or empty: nothing is exported or published, nothing logged
    timeout = 300                      # optional, seconds

After a beat the page is exported into the folder (`room page export`, run through the beat's starter) and, when the
files differ from the last successful publish, `publish` runs once with `{folder}` filled in. A failure or a timeout is one
logged line; it never stops the beat and the page is tried again next beat. Credentials are the tool's own.
"""
from __future__ import annotations

import hashlib
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

FILENAME = "page.toml"
DEFAULT_TIMEOUT = 300
BUILT = "const BUILT = "   # the one line of the export that changes on every build


def settings(home: Path) -> dict | None:
    """`{folder, publish, timeout}` from the room home's page.toml, or None when nothing is configured."""
    path = Path(home) / FILENAME
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("page") or {}
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    publish = table.get("publish")
    if not isinstance(publish, str) or not publish.strip():
        return None
    folder = table.get("folder")
    timeout = table.get("timeout")
    return {"publish": publish.strip(),
            "folder": Path(folder) if isinstance(folder, str) and folder.strip() else Path(home) / "page" / "public",
            "timeout": timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0 else DEFAULT_TIMEOUT}


def fingerprint(folder: Path) -> str:
    """A hash of the exported files with the build-time line left out."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(folder).rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        digest.update("\n".join(line for line in lines if not line.startswith(BUILT)).encode("utf-8") + b"\0")
    return digest.hexdigest()


def _last_line(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def publish(home: Path, project_folder: str, starter, log, python: str | None = None, runner=subprocess.run) -> None:
    """Export, then publish once if the page changed. Never raises."""
    try:
        conf = settings(home)
        if conf is None:
            return
        python = python or sys.executable
        room = Path(__file__).resolve().parent.parent / "room.py"
        proc = starter([python, str(room), "page", "export", str(conf["folder"])], project_folder,
                       Path(home) / "beat-page-export.log", True)
        code = getattr(proc, "returncode", 0)
        if code:
            log(f"beat: page export failed (exit {code}); not published")
            return
        marker = Path(home) / "page" / "published.sha256"
        digest = fingerprint(conf["folder"])
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
            return
        line = conf["publish"].replace("{folder}", str(conf["folder"]))
        argv = shlex.split(line, posix=(sys.platform != "win32"))
        if sys.platform == "win32":   # non-POSIX split keeps the quotes around a quoted word; the program must not see them
            argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a for a in argv]
        if argv:   # without a shell, a bare name only finds .exe files; npx is npx.CMD on Windows
            argv[0] = shutil.which(argv[0]) or argv[0]
        try:
            done = runner(argv, cwd=str(home), capture_output=True, text=True, timeout=conf["timeout"])
        except subprocess.TimeoutExpired:
            log(f"beat: page publish timed out after {conf['timeout']}s")
            return
        if done.returncode != 0:
            log(f"beat: page publish failed (exit {done.returncode}): {_last_line(done.stderr) or _last_line(done.stdout)}")
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(digest + "\n", encoding="utf-8", newline="\n")
        log("beat: page published")
    except Exception as exc:   # the page must never stop a beat
        log(f"beat: page publish error: {type(exc).__name__}: {exc}")
