"""Weekly share transfers, separate from the standing registry (commonroom#242).

pie-raises.json lives beside projects.toml so a temporary decision never rewrites standing shares.
Reads are side-effect free; the next successful raise/clear write drops expired windows.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from commonroom import pie, registry
from commonroom.registry import RegistryError

OWNER = "@owner"  # cannot collide with a registry key


def path(home: Path | None = None) -> Path:
    return (home if home is not None else registry.default_path().parent) / "pie-raises.json"


def load(target: Path | None = None) -> list[dict]:
    target = target if target is not None else path()
    try:
        entries = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("expected a list")
        for e in entries:
            if (not isinstance(e, dict) or not isinstance(e.get("provider"), str)
                    or not isinstance(e.get("project"), str)
                    or not registry.is_number(e.get("resets_at"))
                    or not registry.is_number(e.get("reset_at"))
                    or not isinstance(e.get("from"), dict) or not e["from"]
                    or any(not registry.is_number(v) or v <= 0 for v in e["from"].values())):
                raise ValueError("invalid raise")
        return entries
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RegistryError(f"{target.name}: {exc}") from None


def reset_at(readings: list[dict]) -> float:
    """Identify an early reset even if the reader keeps reporting the same scheduled reset time."""
    high, reset = 0.0, 0.0
    for s in sorted(readings, key=lambda s: (s["first_seen"], s["observed_at"], -s["used_pct"])):
        if s["used_pct"] < high - pie.RESET_DROP:
            reset, high = s["first_seen"], s["used_pct"]
        high = max(high, s["used_pct"])
    return reset


def current(entries: list[dict], win, readings: list[dict]) -> list[dict]:
    epoch = reset_at(readings)
    return [e for e in entries if e["provider"] == win.provider
            and abs(e["resets_at"] - win.resets_at) < 3600 and e["reset_at"] == epoch]


def effective(reg, entries: list[dict]) -> tuple[dict[str, float], float]:
    shares = {key: p.share for key, p in reg.projects.items()}
    shares[OWNER] = reg.owner_share()
    for e in entries:
        if e["project"] not in reg.projects or any(k not in shares for k in e["from"]):
            raise RegistryError("a raise names a removed project; use `room pie raise --clear`")
        for donor, points in e["from"].items():
            shares[donor] -= points
            shares[e["project"]] += points
    if any(v < -1e-9 or v > 100 + 1e-9 for v in shares.values()) or sum(shares.values()) > 100 + 1e-9:
        raise RegistryError("raises no longer fit the registry shares; use `room pie raise --clear`")
    # Remove floating point crumbs at the bounds without creating allowance.
    projects = {k: max(0.0, min(100.0, v)) for k, v in shares.items() if k != OWNER}
    while sum(projects.values()) > 100:
        largest = max(projects, key=projects.get)
        projects[largest] = math.nextafter(projects[largest], 0.0)
    return projects, max(0.0, shares[OWNER])


def transfer(reg, report, entries: list[dict], project: str, points: float, sources: list[str] | None) -> dict:
    if project not in reg.projects:
        raise RegistryError(f"unknown project {project!r}")
    if not registry.is_number(points) or points <= 0:
        raise RegistryError("points must be a finite number above zero")
    donors = list(dict.fromkeys(sources)) if sources is not None else [k for k in reg.projects if k != project]
    donors = [OWNER if k == "owner" and k not in reg.projects else k for k in donors]
    shares, owner = effective(reg, entries)
    shares[OWNER] = owner
    spent = {s.name: s.spent for s in report.rows} | {OWNER: report.owner_use}
    available = {}
    for key in donors:
        if key not in shares or key == project:
            raise RegistryError(f"invalid donor {key!r} for {project!r}")
        available[key] = max(0.0, shares[key] - spent[key])
    total = sum(available.values())
    if points > total or total <= 0:
        where = "the named donors" if sources is not None else "other projects"
        raise RegistryError(f"{report.window.provider}: only {registry.fmt_number(total)} points available "
                            f"from {where}")
    funded = {k: points * (v / total) for k, v in available.items() if v > 0}
    funded = {k: v for k, v in funded.items() if v > 0} or {max(available, key=available.get): points}
    # Make the debit sum exactly the requested amount.
    last = max(funded, key=funded.get)
    funded[last] = points - sum(v for k, v in funded.items() if k != last)
    return {"provider": report.window.provider, "project": project, "resets_at": report.window.resets_at,
            "reset_at": report.reset_at, "from": funded}


def save(entries: list[dict]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entries, indent=2, allow_nan=False) + "\n")
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
