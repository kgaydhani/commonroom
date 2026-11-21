"""The owner's absence, and the part of his slice it lends out (commonroom#255).

`room away until <date>` stores one number beside the registry (`away.json` in the room home). While it is in the
future and none of the owner's own sessions is active, the part of his unassigned slice he won't need is lent to the
projects in proportion to their shares. He needs his slice times the fraction of the weekly window after his return;
the rest is lent. This changes shares and reserves only: it never resumes a paused room, clears a breaker or starts a
dispatcher. With nothing stored, nothing here does anything.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from commonroom import registry, usage
from commonroom.registry import RegistryError

FILENAME = "away.json"
WEEK = 7 * 86400


def path(home: Path | None = None) -> Path:
    return (home if home is not None else registry.default_path().parent) / FILENAME


def load(target: Path | None = None) -> float | None:
    """The stored return time (epoch seconds), or None when none is set. A file that can't be read raises
    RegistryError."""
    target = target if target is not None else path()
    try:
        until = json.loads(target.read_text(encoding="utf-8"))["until"]
        if not registry.is_number(until):
            raise ValueError("until must be a number")
        return float(until)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RegistryError(f"{target.name}: {exc}") from None


def save(until: float, target: Path | None = None) -> None:
    target = target if target is not None else path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"until": until}) + "\n")
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


def clear(target: Path | None = None) -> bool:
    target = target if target is not None else path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


def parse(text: str) -> float:
    """A local date (the start of that day) or date and time, as epoch seconds."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    raise RegistryError(f"can't read {text!r} as a date: use 2026-11-25 or '2026-11-25 18:00'")


def lent(owner_share: float, until: float | None, now: float, resets_at: float, active: bool) -> float:
    """Points of the owner's slice lent at `now`: the slice less what he needs, which is the slice times the fraction of
    the week after his return (none of it if he returns after the reset). Nothing without a return date in the future,
    while his own session is active, or when there is no slice."""
    if until is None or until <= now or active or owner_share <= 0:
        return 0.0
    needs = max(0.0, min(1.0, (resets_at - until) / WEEK))
    return owner_share * (1 - needs)


def owner_is_active(now: float, exclude=(), transcripts: Path | None = None) -> bool:
    """Is any session of the owner's active now (`usage.owner_active`, anywhere on this machine)?"""
    return bool(usage.owner_active(None, 20, projects_dir=transcripts, now=now, exclude=exclude))


def apply(shares: dict[str, float], owner_share: float, points: float) -> tuple[dict[str, float], float, dict[str, float]]:
    """(shares with `points` added in proportion to them, the owner's slice less them, {key: points it got})."""
    total = sum(shares.values())
    if points <= 0 or total <= 0:
        return shares, owner_share, {}
    got = {k: points * v / total for k, v in shares.items() if v > 0}
    return {k: v + got.get(k, 0.0) for k, v in shares.items()}, max(0.0, owner_share - points), got
