"""Worker command guard: refuse shell commands that kill processes by filter (commonroom#18).

Workers run on the owner's PC with the owner's session. `taskkill /FI "WINDOWTITLE eq *"`, `Stop-Process -Name`,
`Get-Process x | Stop-Process`, `pkill` and `killall` can close the owner's work, the dispatcher or other live runs.
A worker may stop only processes it started, by PID: `taskkill /PID <n>` and `Stop-Process -Id <n>` stay allowed.

`room guard-command` is a Claude Code PreToolUse hook: it reads the hook JSON on stdin and exits 2 with the reason
on stderr to block the call. `claude_command` wires it in for every Claude worker.

Only a kill word in command position counts: at the start of a statement, after a pipe or a bracket, or after
launcher words such as `sudo`, `xargs`, `cmd /c` or `powershell -Command`. A quoted string is a command only if the
quote itself is in command position (`powershell -Command "Stop-Process -Name x"`, `bash -c '...'`). So a commit
message or a grep pattern that mentions `taskkill /IM` passes. Text in quotes after a `;`, `&` or `|` is still read
as a command (`git commit -m "fix; pkill x"` is blocked): a false block costs a rephrase, a missed one can close the
owner's apps.
"""
from __future__ import annotations

import json
import re
import sys

LAUNCHERS = {"sudo", "xargs", "exec", "call", "nohup", "env", "start", "command", "cmd", "powershell", "pwsh", "wsl",
             "bash", "sh"}
SEPARATORS = ";&|(){}\n\"'`"
STATEMENT_END = re.compile(r"&&|\|\||[;\n&]")
PIPE = re.compile(r"(?<!\|)\|(?!\|)")
KILL_METHOD = re.compile(r"\.kill\s*\(", re.I)
PROCESS_LOOKUP = re.compile(r"(?<![\w-])(get-process|gps|ps|pgrep|pidof)(?![\w-])", re.I)
WORD = re.compile(r"(?<![\w-])(?:[\w.:~-]*[/\\])*(taskkill|pkill|killall|stop-process|spps|kill)(?:\.exe)?(?![\w-])", re.I)
TASKKILL_FILTER = re.compile(r"(?<![\w])(?:/{1,2}|-)(fi|im)(?![\w])", re.I)
NAME_PARAM = re.compile(r"(?<![\w])-(processname|name|nam|na|n)(?![\w-])", re.I)   # Stop-Process accepts prefixes
LONG_NAME_PARAM = re.compile(r"(?<![\w])-(processname|name)(?![\w-])", re.I)        # bash `kill -n 9` is a signal


def _command_position(before: str) -> bool:
    """Whether a word preceded by `before` runs as a command rather than being an argument or text."""
    cut = max(before.rfind(c) for c in SEPARATORS)
    for token in before[cut + 1:].split():
        name = token.lower().removesuffix(".exe").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if name not in LAUNCHERS and not token.startswith(("-", "/")) and "=" not in token:
            return False
    if cut >= 0 and before[cut] in "\"'`":
        return _command_position(before[:cut])   # a quoted payload runs only if the quote itself is a command's
    return True


def blocked(command: str) -> str | None:
    """Why this shell command must not run, or None if it may."""
    for statement in STATEMENT_END.split(command):
        if KILL_METHOD.search(statement) and PROCESS_LOOKUP.search(statement):
            return "`.Kill()` on processes from a lookup (Get-Process) kills by filter"
    for match in WORD.finditer(command):
        start = match.start()
        if not _command_position(command[:start]):
            continue
        word = match.group(1).lower()
        rest = STATEMENT_END.split(command[match.end():], 1)[0]
        ends = [m.end() for m in STATEMENT_END.finditer(command[:start])]
        statement_before = command[ends[-1] if ends else 0:start]
        if word in ("pkill", "killall"):
            return f"`{word}` kills processes by name"
        if word == "taskkill":
            if TASKKILL_FILTER.search(rest):
                return "`taskkill` with /FI or /IM kills every process that matches a filter"
            continue
        # Stop-Process, its alias spps, and kill (bash, or PowerShell's alias of Stop-Process)
        if (NAME_PARAM if word != "kill" else LONG_NAME_PARAM).search(rest):
            return f"`{match.group(1)}` with -Name kills every process with that name"
        if PROCESS_LOOKUP.search(rest):
            return f"`{match.group(1)}` is fed by a process lookup (Get-Process, pgrep, pidof)"
        if PIPE.search(statement_before) and PROCESS_LOOKUP.search(statement_before):
            return f"`{match.group(1)}` is fed from a process lookup (Get-Process, ps, pgrep) in a pipeline"
    return None


def hook(stdin_text: str) -> tuple[int, str]:
    """PreToolUse hook: (exit code, stderr). 2 blocks the tool call; anything unreadable is let through."""
    try:
        data = json.loads(stdin_text or "{}")
        command = (data.get("tool_input") or {}).get("command")
    except (ValueError, AttributeError):
        return 0, ""
    if data.get("tool_name") not in ("Bash", "PowerShell") or not isinstance(command, str):
        return 0, ""
    reason = blocked(command)
    if not reason:
        return 0, ""
    return 2, (f"Blocked by the Common Room command guard: {reason}. It can close the owner's programs, the dispatcher "
               "or other live runs. Stop only processes you started, by PID: `taskkill /PID <n> /F` or "
               "`Stop-Process -Id <n>`. If this was text rather than a command (a commit message), rephrase it.")


def main() -> int:
    code, message = hook(sys.stdin.read())
    if message:
        print(message, file=sys.stderr)
    return code
