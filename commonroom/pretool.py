"""The PreToolUse hook (commonroom#583): the one place the layer blocks anything (docs/layer.md, limit 7).

In the owner's own sessions it blocks only a tool call that a line of his limits.txt names, and says which line did it
and how to lift it. In a room worker (COMMONROOM_WORKER set, which Codex workers inherit) it runs the command guard
instead, the same one Claude workers have. With an empty list it blocks nothing. On any error it allows and prints
nothing. Exit 2 with the reason on stderr blocks the call, in Claude Code and in Codex."""
from __future__ import annotations

import json
import os
import sys

from commonroom import guard, limits


def hook(stdin_text: str) -> tuple[int, str]:
    try:
        if os.environ.get("COMMONROOM_WORKER"):
            return guard.hook(stdin_text)
        data = json.loads(stdin_text or "{}")
        tool, tool_input = data.get("tool_name"), data.get("tool_input")
        if not isinstance(tool, str) or not isinstance(tool_input, dict):
            return 0, ""
        path = limits.home() / limits.FILENAME
        found = limits.first_match(path, tool, tool_input)
        if not found:
            return 0, ""
        n, rule = found
        return 2, (f"Blocked by your own limit, line {n} of {path}: {rule}. To lift it, delete that line "
                   f"(it takes effect at once; run `room install` to drop its deny rule too).")
    except Exception:   # noqa: BLE001 - a fault in the layer must never stop a session
        return 0, ""


def main() -> int:
    code, message = hook(sys.stdin.read())
    if message:
        print(message, file=sys.stderr)
    return code
