"""`room config show|set`: read and change a project's own `room.toml` (commonroom#451). Standard library only.
A change edits one `key = value` line in place and keeps every other byte, comment and line ending as it was."""
from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from commonroom import config as config_mod, registry

HEADER = re.compile(r"^[ \t]*\[([^\[\]]+)\][ \t]*(?:#.*)?$")


class Refused(ValueError):
    """A change or lookup the room will not make; the message is the one-line reason."""


def project_file(name: str) -> Path:
    try:
        reg = registry.load(check=False)
    except registry.RegistryError as exc:
        raise Refused(str(exc))
    if name not in reg.projects:
        raise Refused(f"no project {name!r} in {registry.default_path()}")
    path = Path(reg.projects[name].path) / "room.toml"
    if not path.is_file():
        raise Refused(f"{path} does not exist")
    return path


def _norm(table: str) -> str:
    return ".".join(part.strip() for part in table.split("."))


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_fmt(v)}" for k, v in value.items()) + "}"
    return str(value)


def _flatten(data: dict, prefix: str = "") -> list[tuple[str, object]]:
    rows = []
    for key, value in data.items():
        if isinstance(value, dict) and value:
            rows += _flatten(value, f"{prefix}{key}.")
        else:
            rows.append((f"{prefix}{key}", value))
    return rows


def show(path: Path, key: str | None = None) -> list[str]:
    """The file's keys as `table.key = value`, one per line; with `key`, that key (or every key under that table)."""
    rows = _flatten(tomllib.loads(path.read_bytes().decode("utf-8")))
    if key is not None:
        rows = [(k, v) for k, v in rows if k == key or k.startswith(key + ".")]
        if not rows:
            raise Refused(f"no key {key!r} in {path}")
    return [f"{k} = {_fmt(v)}" for k, v in rows]


def parse_value(value: str):
    if "\n" in value or "\r" in value:
        raise Refused("the value must be on one line")
    try:
        return tomllib.loads(f"v = {value}")["v"]
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"{value!r} is not a TOML value ({exc}); quote strings, like '\"text\"'")


def _comment_of(rest: str) -> str:
    """The trailing `# comment` of a line's value part, with its leading space, or ''."""
    try:
        old = tomllib.loads(f"v ={rest}")["v"]
    except tomllib.TOMLDecodeError:
        return ""
    for i, ch in enumerate(rest):
        if ch == "#":
            try:
                if tomllib.loads(f"v ={rest[:i]}")["v"] == old:
                    return rest[len(rest[:i].rstrip()):]
            except tomllib.TOMLDecodeError:
                continue
    return ""


def edit(text: str, dotted: str, value: str) -> str:
    """`text` with `table.key` set to the TOML `value`. Raises Refused when it can't be done as one line."""
    table, _, key = dotted.rpartition(".")
    if not table or not key:
        raise Refused(f"{dotted!r} is not a table.key")
    new = parse_value(value)
    data = tomllib.loads(text)
    here = data
    for part in table.split("."):
        here = here.get(part) if isinstance(here, dict) else None
    if isinstance(here, dict) and isinstance(here.get(key), dict):
        raise Refused(f"{dotted} is a table or an inline table; edit it by hand")
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    current, start, end = None, None, len(lines)   # the table's lines are start..end
    for i, line in enumerate(lines):
        m = HEADER.match(line.rstrip("\r\n"))
        if m:
            if current == table and end == len(lines):
                end = i
            if _norm(m.group(1)) == table and start is None:
                start = i + 1
            current = _norm(m.group(1))
        elif line.lstrip().startswith("[["):
            if current == table and end == len(lines):
                end = i
            current = None
    keyline = re.compile(rf"^([ \t]*){re.escape(key)}([ \t]*=[ \t]*)(.*?)([ \t]*)$")
    if start is not None:
        for i in range(start, end):
            m = keyline.match(lines[i].rstrip("\r\n"))
            if not m:
                continue
            rest = lines[i].rstrip("\r\n").split("=", 1)[1]
            try:
                old = tomllib.loads(f"v ={rest}")["v"]
            except tomllib.TOMLDecodeError:
                raise Refused(f"{dotted} has a multi-line value; edit it by hand")
            if isinstance(old, dict):
                raise Refused(f"{dotted} is an inline table; edit it by hand")
            tail = lines[i][len(lines[i].rstrip("\r\n")):]
            lines[i] = f"{m.group(1)}{key}{m.group(2)}{value.strip()}{_comment_of(rest)}{tail or eol}"
            break
        else:
            at = end
            while at > start and not lines[at - 1].strip():
                at -= 1
            if at and not lines[at - 1].endswith("\n"):
                lines[at - 1] += eol
            lines.insert(at, f"{key} = {value.strip()}{eol}")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += eol
        if lines and lines[-1].strip():
            lines.append(eol)
        lines += [f"[{table}]{eol}", f"{key} = {value.strip()}{eol}"]
    return "".join(lines)


def check(path: Path, result: str) -> None:
    """Refuse a result the file no longer parses as, or `config.load` rejects."""
    try:
        tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"the result is not valid TOML ({exc})")
    fd, tmp = tempfile.mkstemp(prefix="room-config-", suffix=".toml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(result.encode("utf-8"))
        config_mod.load(Path(tmp), follow_host=False)
    except Exception as exc:   # ConfigError, or anything the loader raises on a bad value
        raise Refused(f"config.load rejects the result: {exc}")
    finally:
        os.unlink(tmp)


def set_value(path: Path, dotted: str, value: str, write: bool) -> str:
    """The unified diff of the change; with `write`, the file is replaced by the result."""
    before = path.read_bytes().decode("utf-8")
    after = edit(before, dotted, value)
    check(path, after)
    diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), "room.toml", "room.toml (changed)", lineterm=""))
    if write and after != before:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(after.encode("utf-8"))
        os.replace(tmp, path)
    return diff
