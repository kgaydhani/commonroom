"""The other two things `room install` does (commonroom#567): Codex hooks and the scheduled beat.

Codex: `hooks.json` in CODEX_HOME (or ~/.codex), the user-level file Codex reads (docs/scout/codex-hooks.md). Install
adds a SessionStart and a UserPromptSubmit entry that run the plugin's launcher by absolute path (Codex has no `args`
field, so the command is one string). Uninstall removes only the entries install added. Codex asks the owner to trust
new hooks once, in /hooks.

Beat: the command `room schedule` prints is the one install runs, and uninstall deletes the task or cron line. Where
it cannot be registered (no scheduler program, or it refuses), install prints the command and says so."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from commonroom import doctor, schedule

EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "prompt-submit", "PreToolUse": "pre-tool"}


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def run(command: str, stdin: str | None = None) -> tuple[int, str]:
    """Run one scheduler command through the shell: (exit code, output). A missing program is exit 127."""
    try:
        r = subprocess.run(command, shell=True, input=stdin, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def hook_command(root: Path, event: str) -> str:
    return f'python "{(root / "plugin" / "hooks" / "run.py").as_posix()}" {EVENTS[event]}'


def _ours(group: dict, root: Path, event: str) -> bool:
    return any(h.get("command") == hook_command(root, event) for h in group.get("hooks", []) if isinstance(h, dict))


def codex_plan(path: Path, root: Path) -> tuple[dict, list[str], bool]:
    """(new hooks.json content, the events to add, whether the file is absent)."""
    raw = path.read_text(encoding="utf-8") if path.exists() else None
    data = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"{path} is not a hooks file")
    added = []
    for event in EVENTS:
        groups = data.setdefault("hooks", {}).setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"{path}: hooks.{event} is not a list")
        if not any(isinstance(g, dict) and _ours(g, root, event) for g in groups):
            group = {"hooks": [{"type": "command", "command": hook_command(root, event), "timeout": 5}]}
            if event == "PreToolUse":
                group["matcher"] = "Bash"   # the command guard for workers; Codex's shell tool
            groups.append(group)
            added.append(event)
    return data, added, raw is None


def codex_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def codex_remove(path: Path, root: Path, created: bool) -> bool:
    """Take our entries out; delete the file if install made it and nothing else is left. True if it changed."""
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    hooks, changed = data.get("hooks", {}), False
    for event in EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        keep = [g for g in groups if not (isinstance(g, dict) and _ours(g, root, event))]
        if len(keep) != len(groups):
            changed = True
            if keep:
                hooks[event] = keep
            else:
                del hooks[event]
    if not changed:
        return False
    if not hooks:
        data.pop("hooks", None)
    if created and not data:
        path.unlink()
    else:
        codex_write(path, data)
    return True


def beat_command(root: Path) -> tuple[str, str]:
    return schedule.build(sys.platform, str(root))


def beat_registered(root: Path, runner=None) -> bool:
    _, check = beat_command(root)
    code, out = (runner or run)(check)
    if code != 0:
        return False
    return True if sys.platform.startswith("win") else f"{(root / 'room.py').as_posix()}" in out and " beat" in out


def old_beats(root: Path, runner=None) -> list[str]:
    """Names of enabled tasks other than ours that already run this checkout's heartbeat or beat script (Windows only).
    Registering CommonRoomBeat beside them would beat every project twice."""
    if not sys.platform.startswith("win"):
        return []
    run_ = runner or run
    _, _, names = doctor.beat_scheduled(root, lambda args: run_(" ".join(args)), True, all_names=True)
    return [n.lstrip("\\") for n in names if n.lstrip("\\") != schedule.TASK_NAME]


def beat_register(root: Path, runner=None) -> tuple[bool, str]:
    command, _ = beat_command(root)
    code, out = (runner or run)(command)
    return code == 0, out.strip()


def beat_unregister(root: Path, runner=None) -> bool:
    runner = runner or run
    if sys.platform.startswith("win"):
        return runner(f"schtasks /delete /tn {schedule.TASK_NAME} /f")[0] == 0
    code, out = runner("crontab -l")
    if code != 0:
        return False
    needle = (root / "room.py").as_posix()
    lines = out.splitlines()
    keep = [ln for ln in lines if not (needle in ln and " beat" in ln)]
    if len(keep) == len(lines):
        return False
    return runner("crontab -", "\n".join(keep) + ("\n" if keep else ""))[0] == 0
