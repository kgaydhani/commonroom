"""Where each registered project stands against its share of one provider's weekly allowance, read from the ledger
and the registry (commonroom#238).

Read-only: it shows standings and gates nothing. Every function is given what it reads (the ledger, the registry, the
provider and `now`): no clock, no global. The arithmetic is `commonroom.pie`'s; this module only picks its inputs. Every
spend figure is an ESTIMATE, because the meter is plan-wide (see `pie.estimate_spend`).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from commonroom import config as config_mod, pie
from commonroom.liveness import orphaned
from commonroom.registry import Registry, RegistryError

WINDOW = "seven_day"
# Readings of one weekly window carry the same reset time give or take seconds; another window's is days away.
SAME_WINDOW = 3600


@dataclass(frozen=True)
class Window:
    provider: str
    resets_at: float
    elapsed: float          # the part of the week gone at `now`, 0 to 1
    used_pct: float         # the plan-wide meter at the latest reading
    observed_at: float      # when that reading was last seen (no later than `now`)


@dataclass(frozen=True)
class Report:
    window: Window
    rows: list[pie.Standing]                            # one per registered project, by registry key
    owner_share: float                                  # the owner's unassigned slice
    unregistered: dict = field(default_factory=dict)    # room projects in the ledger but not the registry: estimates

    @property
    def projects_total(self) -> float:
        """The registered projects' estimated spend."""
        return sum(s.spent for s in self.rows)

    @property
    def owner_use(self) -> float:
        """The owner's estimated own use: the plan-wide figure less every room project's estimated spend."""
        return max(0.0, self.window.used_pct - self.projects_total - sum(self.unregistered.values()))


def window(ledger, provider: str, now: float) -> Window | None:
    """The weekly window of the latest `seven_day` reading at `now`, or None with no reading or once it has reset."""
    rows = [s for s in ledger.usage_history(provider, WINDOW, now - pie.WEEK) if s["first_seen"] <= now]
    if not rows:
        return None
    latest = max(rows, key=lambda s: (min(s["observed_at"], now), s["first_seen"]))
    resets = latest["resets_at"]
    if resets is None or resets <= now:
        return None
    return Window(provider, resets, pie.elapsed_fraction(now, resets), latest["used_pct"], min(latest["observed_at"], now))


def samples(ledger, win: Window, now: float) -> list[dict]:
    """The window's own readings up to `now`. A reading with another window's reset time (a stale reader after an early
    reset) is left out."""
    return [s for s in ledger.usage_history(win.provider, WINDOW, win.resets_at - pie.WEEK)
            if s["first_seen"] <= now and s["resets_at"] is not None and abs(s["resets_at"] - win.resets_at) < SAME_WINDOW]


def runs(ledger, win: Window, now: float) -> list[dict]:
    """Every project's runs on the window's provider from its start to `now`. Runs with no model are left out, and so
    are open runs whose dispatcher has died, which would otherwise count as active up to `now`."""
    return [r for r in ledger.runs_during(win.provider, win.resets_at - pie.WEEK, now)
            if r["model"] is not None and not (r["ended_at"] is None and orphaned(r, now))]


def ledger_names(reg: Registry) -> dict[str, str]:
    """{registry key: the name its runs carry in the ledger}: the `[project] name` in the room.toml at its path, or the
    key when that has no name."""
    names = {}
    for key, p in reg.projects.items():
        path = Path(p.path) / "room.toml"
        try:
            cfg = config_mod.load(path, follow_host=False)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RegistryError(f"project {key!r}: can't read {path}: {exc}") from None
        names[key] = cfg["project"].get("name") or key
    return names


def standings(ledger, reg: Registry, provider: str, now: float) -> Report | None:
    """The registry's projects' standings on `provider` at `now`, or None when there is no current weekly window."""
    win = window(ledger, provider, now)
    if win is None:
        return None
    spent = pie.estimate_spend(samples(ledger, win, now), runs(ledger, win, now), now)
    names = ledger_names(reg)
    projects = {key: {"share": p.share, "when": p.when, "strict": p.strict} for key, p in reg.projects.items()}
    by_key = {key: spent.get(name, 0.0) for key, name in names.items()}
    slack = config_mod.DEFAULTS["budget"].get(provider, {}).get("weekly_slack", 0)
    rows = pie.standings(projects, by_key, win.elapsed, slack)
    known = set(names.values())
    return Report(win, rows, reg.owner_share(), {name: pct for name, pct in spent.items() if name not in known})
