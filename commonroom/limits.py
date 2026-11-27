"""The owner's optional Claude Code deny rules (commonroom#582). No defaults are active."""
from pathlib import Path
import os
import re

FILENAME = "limits.txt"
TEMPLATE = Path(__file__).parent / "ways" / "templates" / FILENAME


def load(path: Path) -> list[str]:
    text = (path if path.exists() else TEMPLATE).read_text(encoding="utf-8")
    rules = []
    for n, line in enumerate(text.splitlines(), 1):
        rule = line.strip()
        if not rule or rule.startswith("#"):
            continue
        if not re.fullmatch(r"[^\s()]+(?:\(.+\))?", rule):
            raise ValueError(f"{path}:{n}: expected Tool or Tool(specifier)")
        if rule not in rules:
            rules.append(rule)
    return rules


def plan(data: dict, rules: list[str], owned: list[str]) -> tuple[list[str], list[str]]:
    """Merge rules, removing only previously installed ones absent from the list."""
    permissions = data.get("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("permissions must be a JSON object")
    deny = permissions.get("deny", [])
    if not isinstance(deny, list) or any(not isinstance(x, str) for x in deny):
        raise ValueError("permissions.deny must be a list of strings")
    removed = [r for r in owned if r not in rules and r in deny]
    added = [r for r in rules if r not in deny]
    if added or removed:
        keep = list(deny)
        for rule in removed:
            keep.remove(rule)
        data.setdefault("permissions", {})["deny"] = keep + added
    return added, removed


def remove(data: dict, entry: dict) -> bool:
    _, removed = plan(data, [], entry.get("deny_added", []))
    changed = bool(removed)
    permissions = data.get("permissions", {})
    if entry.get("deny_created") and permissions.get("deny") == []:
        del permissions["deny"]
        changed = True
    if "permissions" in entry.get("created", []) and not permissions:
        data.pop("permissions", None)
        changed = True
    return changed


def home() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom")


def numbered(path: Path) -> list[tuple[int, str]]:
    """The active (line number, rule) pairs of the owner's list. A missing file is an empty list."""
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        rule = line.strip()
        if rule and not rule.startswith("#") and re.fullmatch(r"[^\s()]+(?:\(.+\))?", rule):
            out.append((n, rule))
    return out


def _case_insensitive() -> bool:
    return os.name == "nt"


def statements(command: str) -> list[str]:
    """The statements of a shell command, split on && || ; | & and newlines outside quotes."""
    out, cur, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(command):
                i += 1
                cur.append(command[i])
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in ";|&\n":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _glob(pattern: str, text: str, path: bool) -> bool:
    if path:
        pattern, text = os.path.expanduser(pattern).replace("\\", "/"), text.replace("\\", "/")
        regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    else:
        if pattern.endswith(":*") and text == pattern[:-2]:
            return True
        pattern = pattern[:-2] + " *" if pattern.endswith(":*") else pattern
        regex = re.escape(pattern).replace(r"\*", ".*")
    flags = re.S | (re.I if path and _case_insensitive() else 0)
    return re.fullmatch(regex, text, flags) is not None


def matches(rule: str, tool: str, tool_input: dict) -> bool:
    """Whether a Claude Code permission rule (Tool or Tool(specifier)) names this tool call."""
    name, _, rest = rule.partition("(")
    spec = rest[:-1] if rest else None
    if name != tool:
        return False
    if tool in ("Bash", "PowerShell"):
        command = tool_input.get("command")
        if not isinstance(command, str):
            return False
        return spec is None or any(_glob(spec, c, False) for c in [command.strip(), *statements(command)])
    if spec is None:
        return True
    target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    return isinstance(target, str) and _glob(spec, target, True)


def first_match(path: Path, tool: str, tool_input: dict) -> tuple[int, str] | None:
    return next(((n, r) for n, r in numbered(path) if matches(r, tool, tool_input)), None)
