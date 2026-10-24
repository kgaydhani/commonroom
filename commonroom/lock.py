"""Pid lock files: one holder at a time, and a lock left by a dead process is taken over."""
from __future__ import annotations

import os
import subprocess
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
