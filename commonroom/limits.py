"""The owner's optional Claude Code deny rules (commonroom#582). No defaults are active."""
from pathlib import Path
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
