"""Is a 'running' row's dispatcher still alive? Shared by the dispatcher and the pie's standings, which must not import
the dispatcher (commonroom#238)."""
from __future__ import annotations

import os
import subprocess

ORPHAN_AGE = 7200   # a worker never runs past an hour; a run 'running' for longer has lost its dispatcher


def pid_alive(pid: int) -> bool:
    """Is this dispatcher process still running? Errs towards yes: wrongly closing a live run is worse."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FI", "IMAGENAME eq python.exe", "/NH"],
                                 capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def orphaned(run: dict, now: float) -> bool:
    """A 'running' row whose dispatcher is gone: older than ORPHAN_AGE, or its pid (not ours) is dead."""
    dead = run.get("pid") and run["pid"] != os.getpid() and not pid_alive(run["pid"])
    return bool(dead) or now - run["started_at"] >= ORPHAN_AGE
