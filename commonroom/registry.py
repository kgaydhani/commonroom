"""The registry of projects the room serves, each with a share of the weekly allowance (commonroom#225).

It is a plain file the owner can read and edit, `<room home>/projects.toml`, where the room home is the folder that
holds the ledger (`COMMONROOM_HOME`, or `~/.commonroom`). A missing file is an empty registry. The owner's own slice
is whatever the shares leave, `100 - sum(shares)`: it is derived, never stored.

Nothing in the room reads the registry yet, so it changes no project's behaviour. The arithmetic over it is in
`commonroom.pie`.
"""
from __future__ import annotations

import math
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from commonroom import ledger as ledger_mod

FILENAME = "projects.toml"
WORK = ("fixed", "open")
WHEN = ("paced", "spare")
NAME = re.compile(r"[A-Za-z0-9_-]+")          # a bare TOML key, so `[projects.<name>]` needs no quoting
PROJECT_KEYS = ("path", "share", "work", "when", "strict")   # the order they are written in
BUDGET_KEYS = ("reserve_5h",)

HEADER = """\
# Common Room: the projects this room serves, and each one's share of the weekly allowance.
# `room projects` rewrites this file and keeps only this header; comments added by hand are not kept.
#
# [budget]
# reserve_5h  percent of each 5-hour window kept for the owner
#
# [projects.<name>]
# path    the project's folder. It must hold a room.toml.
# share   percent of the weekly allowance, from 0 to 100. Zero is off. Shares sum to at most 100, and what
#         they leave is the owner's own slice.
# work    "fixed": only scheduled and explicitly added work. "open": the planner may add work.
# when    "paced": up to its share. "spare": only allowance nobody else wants.
# strict  true: its unused share is never lent to other projects.
"""


class RegistryError(ValueError):
    """A registry that can't be read, or that breaks a rule. The message names the project and the key."""


@dataclass
class Project:
    name: str
    path: str
    share: float
    work: str = "open"
    when: str = "paced"
    strict: bool = False


@dataclass
class Registry:
    projects: dict[str, Project] = field(default_factory=dict)
    budget: dict = field(default_factory=dict)

    def owner_share(self) -> float:
        """The owner's own slice: what the shares leave."""
        return 100 - sum(p.share for p in self.projects.values() if is_number(p.share))


def default_path() -> Path:
    return ledger_mod.default_path().parent / FILENAME


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse(text: str) -> Registry:
    """Read the file's text into a Registry without checking the rules (see `problems`)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{FILENAME} is not valid TOML: {exc}") from None
    unknown = sorted(set(data) - {"budget", "projects"})
    if unknown:
        raise RegistryError(f"{FILENAME}: unknown table or key {unknown[0]!r}")
    budget, projects = data.get("budget", {}), data.get("projects", {})
    if not isinstance(budget, dict) or not isinstance(projects, dict):
        raise RegistryError(f"{FILENAME}: [budget] and [projects.<name>] must be tables")
    for key in budget:
        if key not in BUDGET_KEYS:
            raise RegistryError(f"[budget]: unknown key {key!r}")
    reg = Registry(budget=dict(budget))
    for name, table in projects.items():
        if not isinstance(table, dict):
            raise RegistryError(f"project {name!r}: must be a table [projects.{name}]")
        for key in table:
            if key not in PROJECT_KEYS:
                raise RegistryError(f"project {name!r}: unknown key {key!r}")
        for key in ("path", "share"):
            if key not in table:
                raise RegistryError(f"project {name!r}: missing key {key!r}")
        reg.projects[name] = Project(name, **table)
    return reg


def problems(reg: Registry) -> list[str]:
    """Every rule the registry breaks, each naming the project and the key. Empty means it is valid."""
    found = []
    reserve = reg.budget.get("reserve_5h")
    if reserve is not None and not (is_number(reserve) and 0 <= reserve <= 100):
        found.append(f"[budget]: 'reserve_5h' must be a number from 0 to 100, not {reserve!r}")
    seen: dict[str, str] = {}
    for name, p in reg.projects.items():
        if not NAME.fullmatch(name):
            found.append(f"project {name!r}: the name may hold only letters, digits, '-' and '_'")
        if name.lower() in seen:
            found.append(f"project {name!r}: the name is already used by {seen[name.lower()]!r}")
        seen.setdefault(name.lower(), name)
        if not isinstance(p.path, str) or not p.path:
            found.append(f"project {name!r}: 'path' must be a folder, not {p.path!r}")
        elif not Path(p.path).is_dir():
            found.append(f"project {name!r}: 'path' {p.path} does not exist")
        elif not (Path(p.path) / "room.toml").is_file():
            found.append(f"project {name!r}: 'path' {p.path} holds no room.toml")
        if not (is_number(p.share) and 0 <= p.share <= 100):
            found.append(f"project {name!r}: 'share' must be a number from 0 to 100, not {p.share!r}")
        if p.work not in WORK:
            found.append(f"project {name!r}: 'work' must be {' or '.join(map(repr, WORK))}, not {p.work!r}")
        if p.when not in WHEN:
            found.append(f"project {name!r}: 'when' must be {' or '.join(map(repr, WHEN))}, not {p.when!r}")
        if not isinstance(p.strict, bool):
            found.append(f"project {name!r}: 'strict' must be true or false, not {p.strict!r}")
    total = sum(p.share for p in reg.projects.values() if is_number(p.share))
    if total > 100:
        found.append(f"shares sum to {fmt_number(total)}, more than 100")
    return found


def load(path: Path | str | None = None, check: bool = True) -> Registry:
    """The registry at `path` (default: the room home's). A missing file is an empty registry. With `check`, a file
    that breaks a rule raises RegistryError listing every problem."""
    path = Path(path) if path else default_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Registry()
    reg = parse(text)
    if check and (found := problems(reg)):
        raise RegistryError("; ".join(found))
    return reg


def _string(value: str) -> str:
    """A TOML basic string: backslashes, quotes and control characters escaped, so Windows paths round-trip."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch in "\b\t\n\f\r":
            out.append({"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _string(value)
    return fmt_number(value)


def dumps(reg: Registry) -> str:
    """The file's text: the header, then [budget], then each project by name, keys in a fixed order."""
    lines = [HEADER]
    budget = [key for key in BUDGET_KEYS if reg.budget.get(key) is not None]
    if budget:
        lines += ["[budget]", *(f"{key} = {_value(reg.budget[key])}" for key in budget), ""]
    for name in sorted(reg.projects):
        p = reg.projects[name]
        lines += [f"[projects.{name}]", *(f"{key} = {_value(getattr(p, key))}" for key in PROJECT_KEYS), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def save(reg: Registry, path: Path | str | None = None) -> Path:
    """Write the registry atomically (a temporary file, then a replace), LF line endings. A registry that breaks a
    rule raises RegistryError and the file is left as it was."""
    if found := problems(reg):
        raise RegistryError("; ".join(found))
    path = Path(path) if path else default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=FILENAME + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(dumps(reg))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
