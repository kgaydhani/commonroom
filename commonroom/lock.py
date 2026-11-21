"""Pid lock files: one holder at a time, and a lock left by a dead process is taken over."""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87


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


def _query_process(pid: int) -> bool | None:
    """Ask Windows directly whether the process is running. None when it can't answer."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False if ctypes.GetLastError() == _ERROR_INVALID_PARAMETER else None
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return None
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return None


def alive(pid: int) -> bool:
    """Is the process running? When tasklist itself fails (the Codex sandbox denies it), ask Windows through ctypes;
    if that can't answer either, say alive: taking a live holder's lock is worse than waiting on a dead one."""
    try:
        done = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                              capture_output=True, text=True)
        failed = done.returncode != 0 or "ERROR" in (done.stdout or "") or "ERROR" in (done.stderr or "")
        out = done.stdout or ""
    except Exception:
        failed, out = True, ""
    if failed:
        answer = _query_process(pid)
        return True if answer is None else answer
    return any(len(f := row.split('","')) > 1 and f[1].strip('"') == str(pid) for row in out.splitlines())


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


def suite_lock(home: Path, project: str | None) -> Path:
    """The per-project lock every run of the project's test suite takes: `room.py check` and the room's own runs."""
    return Path(home) / "locks" / f"{project or 'default'}-suite.lock"


def release_lock(path: Path) -> None:
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass
