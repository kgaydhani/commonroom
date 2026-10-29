"""Pid lock files: one holder at a time, and a lock left by a dead process is taken over."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


def acquire_lock(path: Path) -> bool:
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            if pid != os.getpid() and alive(pid):
                return False
        except ValueError:
            pass
    path.write_text(str(os.getpid()))
    return True


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def claim_lock(path: Path) -> int | None:
    """Take the lock atomically. Returns None when taken, else the pid of the live holder (0 if it is still being
    written). A lock whose holder is dead is taken over at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(path.read_text().strip())
            except FileNotFoundError:
                continue
            except ValueError:
                try:
                    if time.time() - path.stat().st_mtime < 2:
                        return 0
                except FileNotFoundError:
                    continue
                pid = -1
            if pid == os.getpid() or (pid > 0 and alive(pid)):
                return pid
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return None


def release_lock(path: Path) -> None:
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass
