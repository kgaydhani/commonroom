"""`room beat`: one heartbeat over every registered project (commonroom#247).

`heartbeat.cmd` runs for one host project and carries Common Room along as its guest. With a registry there is no
host: one beat takes one meter probe, then for each registered project, in its folder, starts what the heartbeat
starts for a host today: services, `tick`, a housekeeping loop and one dispatcher per provider and slot the project's
`[parallel]` allows. A project with a share of zero gets everything but the dispatchers. A project whose folder or
`room.toml` is missing is skipped with one logged line. One lock keeps two beats from overlapping.

The processes are started through a `starter(argv, cwd, log, wait)` so tests can fake it; `wait=True` runs to the end.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from commonroom import config as config_mod, pagepublish, pending, registry
from commonroom.lock import claim_lock

ROOM = Path(__file__).resolve().parent.parent / "room.py"
PROVIDERS = ("claude", "codex")
DEFAULT_SLOTS = {"claude": 2, "codex": 2}   # codex has no `[parallel]` default: the heartbeat runs two for its host


def start_process(argv: list[str], cwd: str, log: Path, wait: bool = False):
    """Run `argv` in `cwd`, appending its output to `log`. Returns the Popen. No `COMMONROOM_HOST`: nobody is host."""
    env = {k: v for k, v in os.environ.items() if k != config_mod.HOST_ENV}
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, env=env)
    if wait:
        proc.wait()
    return proc


def plan(name: str, share, cfg: dict) -> list[tuple[str, list[str], bool]]:
    """What one project's beat starts: (log label, room.py arguments, wait for it before going on)."""
    steps = [("services", ["services"], True), ("tick", ["tick"], True)]
    if share and share > 0:
        par = cfg.get("parallel") or {}
        for provider in PROVIDERS:
            for slot in range(1, int(par.get(f"{provider}_slots", DEFAULT_SLOTS[provider])) + 1):
                args = ["run", "--until-idle", "--provider", provider] + (["--slot", str(slot)] if slot > 1 else [])
                steps.append((provider if slot == 1 else f"{provider}{slot}", args, False))
    steps.append(("housekeep", ["housekeep"], False))
    return steps


def beat(home: Path, reg: registry.Registry | None = None, starter=start_process, log=print, python: str | None = None,
         now: float | None = None) -> int:
    """One beat. Returns an exit code: 0 for a beat that ran (or had nothing to run), 1 for an unreadable registry."""
    python = python or sys.executable
    home = Path(home)
    try:
        reg = reg if reg is not None else registry.load(home / registry.FILENAME, check=False)
    except registry.RegistryError as exc:
        log(f"beat: registry unreadable: {exc}")
        return 1
    if not reg.projects and not pending.path(home).exists():
        log(f"beat: no projects are registered in {home / registry.FILENAME}; nothing started")
        return 0
    lock = home / "locks" / "beat.lock"
    holder = claim_lock(lock)
    if holder is not None:
        log(f"beat: another beat holds {lock} (pid {holder}); nothing started")
        return 0
    children = []
    first = None
    try:
        changed = pending.apply_due(home, time.time() if now is None else now, log)
        try:
            if changed:
                reg = registry.load(home / registry.FILENAME, check=False)
        except registry.RegistryError as exc:
            log(f"beat: registry unreadable: {exc}")
            return 1
        if not reg.projects:
            log(f"beat: no projects are registered in {home / registry.FILENAME}; nothing started")
            return 0
        room = [python, str(ROOM)]
        children.append(starter(room + ["probe"], str(home), home / "beat-probe.log", True))
        for name, project in reg.projects.items():
            folder = Path(project.path) if isinstance(project.path, str) else None
            if folder is None or not (folder / "room.toml").is_file():
                log(f"beat: skipping {name}: {project.path} has no room.toml")
                continue
            share = project.share if registry.is_number(project.share) else 0
            try:
                cfg = config_mod.load(folder / "room.toml", follow_host=False)
            except Exception as exc:
                log(f"beat: skipping {name}: room.toml unreadable: {exc}")
                continue
            first = first or str(folder)
            for label, args, wait in plan(name, share, cfg):
                proc = starter(room + args, str(folder), home / f"beat-{name}-{label}.log", wait)
                if not wait:
                    children.append(proc)
        for proc in children:
            if hasattr(proc, "wait"):
                proc.wait()
        if first:
            pagepublish.publish(home, first, starter, log, python=python)
        return 0
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
