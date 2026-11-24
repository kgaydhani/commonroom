"""`room schedule`: print the command that registers the beat with the platform's scheduler. Runs and writes nothing."""
from __future__ import annotations

import shlex
import sys
from pathlib import Path, PureWindowsPath

DEFAULT_MINUTES = 15
TASK_NAME = "CommonRoomBeat"


def build(platform: str, checkout: str, minutes: int = DEFAULT_MINUTES) -> tuple[str, str]:
    """(the command to run, the line that checks it is registered) for `platform` (a `sys.platform` value)."""
    if not 1 <= minutes <= 1440:
        raise ValueError(f"--minutes must be from 1 to 1440, not {minutes}")
    if platform.startswith("win"):
        vbs = str(PureWindowsPath(checkout) / "beat.vbs")
        # schtasks accepts /sc minute /mo 1-1439 only, so a whole day is /sc daily. The script path is quoted
        # inside /tr (as \") so a checkout path with spaces still reaches wscript as one argument.
        every = "/sc daily" if minutes == 1440 else f"/sc minute /mo {minutes}"
        create = f'schtasks /create {every} /tn {TASK_NAME} /tr "wscript.exe \\"{vbs}\\""'
        return create, f"schtasks /query /tn {TASK_NAME}"
    if minutes < 60:
        when = f"*/{minutes} * * * *"
    elif minutes % 60 == 0:
        when = "0 0 * * *" if minutes == 1440 else f"0 */{minutes // 60} * * *"
    else:
        raise ValueError("on Linux and macOS --minutes above 59 must be a whole number of hours")
    room = shlex.quote((Path(checkout) / "room.py").as_posix())
    line = f"{when} python3 {room} beat >> $HOME/.commonroom/beat.log 2>&1"
    return f"(crontab -l 2>/dev/null; echo {shlex.quote(line)}) | crontab -", "crontab -l"


def cmd_schedule(args) -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        command, check = build(sys.platform, str(root), args.minutes)
    except ValueError as e:
        print(f"refused: {e}")
        return 2
    print("Run this once yourself; the room never changes the scheduler:")
    print(command)
    print(f"Check it is registered: {check}")
    return 0
