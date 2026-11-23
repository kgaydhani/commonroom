"""Scheduled registry changes, held beside projects.toml until the beat applies them (#488)."""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from commonroom import registry
from commonroom.lock import claim_lock, release_lock
from commonroom.registry import RegistryError

FILENAME = "projects-pending.json"
FIELDS = ("share", "work", "when", "strict")


def path(home: Path | None = None) -> Path:
    return (home if home is not None else registry.default_path().parent) / FILENAME


def parse_time(text: str) -> float:
    """ISO 8601 with a zone, or local YYYY-MM-DD HH:MM, stored as epoch seconds."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", text):
            return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()
        date = datetime.fromisoformat(text)
        if date.tzinfo is not None:
            return date.timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    raise RegistryError(f"can't read {text!r} as a time: use ISO 8601 with a zone or local YYYY-MM-DD HH:MM")


def clock(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")


def load(target: Path | None = None) -> list[dict]:
    target = target if target is not None else path()
    try:
        entries = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("expected a list")
        for e in entries:
            if (not isinstance(e, dict) or not isinstance(e.get("project"), str)
                    or not registry.is_number(e.get("at")) or not isinstance(e.get("changes"), dict)
                    or not e["changes"] or set(e["changes"]) - set(FIELDS)):
                raise ValueError("invalid pending change")
        return entries
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RegistryError(f"{target.name}: {exc}") from None


def _save(entries: list[dict], target: Path) -> None:
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entries, indent=2, allow_nan=False) + "\n")
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def _writer(target: Path):
    lock = target.with_suffix(".lock")
    if claim_lock(lock) is not None:
        raise RegistryError(f"{target.name}: a write is in progress; try again next beat")
    try:
        yield
    finally:
        release_lock(lock)


def schedule(name: str, at: str, changes: dict) -> None:
    timestamp = parse_time(at)  # an unreadable time writes nothing, including a lock or directory
    if not changes or set(changes) - set(FIELDS):
        raise RegistryError("--at needs at least one field to change")
    if any(isinstance(v, float) and not registry.is_number(v) for v in changes.values()):
        raise RegistryError("pending values must be finite")
    if name not in registry.load(check=False).projects:
        raise RegistryError(f"no project {name!r} in {registry.default_path()}")
    target = path()
    with _writer(target):
        entries = load(target)
        entries.append({"project": name, "at": timestamp, "changes": changes})
        _save(entries, target)


def cancel(name: str) -> int:
    target = path()
    with _writer(target):
        entries = load(target)
        kept = [e for e in entries if e["project"] != name]
        if len(kept) != len(entries):
            _save(kept, target)
        return len(entries) - len(kept)


def apply_due(home: Path, now: float, log=print) -> bool:
    """Apply in time order. Refusals are consumed and logged; pending-file errors leave the beat running."""
    target, changed = path(home), False
    if not target.exists():
        return False
    try:
        with _writer(target):
            entries = sorted(load(target), key=lambda e: e["at"])
            while entries and entries[0]["at"] <= now:
                entry = entries[0]
                name, changes = entry["project"], entry["changes"]
                try:
                    project = registry.load(home / registry.FILENAME, check=False).projects.get(name)
                    before = {key: getattr(project, key, None) for key in changes}
                    result = registry.set_project(name, path=home / registry.FILENAME, **changes)
                except RegistryError as exc:
                    log(f"beat: pending {name} {changes!r} refused: {exc}")
                else:
                    changed = True
                    values = ", ".join(f"{key} {before[key]!r} -> {value!r}" for key, value in changes.items())
                    warnings = "; ".join(result["warnings"])
                    log(f"beat: pending {name}: {values}" + (f"; warning: {warnings}" if warnings else ""))
                entries.pop(0)
                _save(entries, target)
    except (RegistryError, OSError) as exc:
        log(f"beat: pending changes not processed: {exc}")
    return changed
