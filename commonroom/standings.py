"""Where each registered project stands against its share of one provider's weekly allowance, read from the ledger
and the registry (commonroom#238).

Read-only: it computes standings, and `dispatch.pie_holds` gates new model work on them (commonroom#240). Every function is given what it reads (the ledger, the registry, the
provider and `now`), with weekly raises read beside the supplied ledger: no clock or live ledger. The arithmetic is
`commonroom.pie`'s; this module only picks its inputs. Every
spend figure is an ESTIMATE, because the meter is plan-wide (see `pie.estimate_spend`).

On Claude, the owner's own sessions in a project's folder count as that project's, beside its room runs
(commonroom#239): `commonroom.sessions` reads them from Claude Code's transcripts.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from commonroom import away, config as config_mod, pie, raises, sessions
from commonroom.liveness import orphaned
from commonroom.registry import Registry, RegistryError

WINDOW = "seven_day"
OWN = "own sessions"
# The providers whose interactive sessions `commonroom.sessions` can read.
SESSION_PROVIDERS = ("claude",)
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
    own: dict = field(default_factory=dict)             # {registry key: the part of its spend from the owner's sessions}
    raises: list[dict] = field(default_factory=list)    # transfers in this provider's current window
    reset_at: float = 0.0                               # early reset inside the scheduled window
    own_last: dict = field(default_factory=dict)        # {registry key: when its owner's latest session activity ended}
    away_until: float | None = None                     # the owner's stored return time, while it is in the future
    lent: dict = field(default_factory=dict)            # {registry key: points of his slice lent to it while he is away}

    def room(self, key: str) -> float:
        """A project's estimated spend by room runs: its total less its own sessions'."""
        return next(s.spent for s in self.rows if s.name == key) - self.own.get(key, 0.0)

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


NO_NAME = "(no project name)"


def ledger_names(reg: Registry) -> dict[str, str | None]:
    """{registry key: the name its runs carry in the ledger}: the `[project] name` in the room.toml at its path, or None
    when it has none, because the dispatcher then records its runs with a NULL project. Two keys that resolve to one
    name would each be given the whole spend, so that is refused."""
    names: dict[str, str | None] = {}
    for key, p in reg.projects.items():
        path = Path(p.path) / "room.toml"
        try:
            cfg = config_mod.load(path, follow_host=False)
        except (OSError, tomllib.TOMLDecodeError, config_mod.ConfigError) as exc:
            raise RegistryError(f"project {key!r}: can't read {path}: {exc}") from None
        name = cfg["project"].get("name") or None
        for other, taken in names.items():
            if taken == name:
                raise RegistryError(f"projects {other!r} and {key!r} both write runs as {name or 'no name'!r}")
        names[key] = name
    return names


def own_runs(ledger, reg: Registry, win: Window, now: float, transcripts: Path | None = None) -> list[dict]:
    """The owner's own sessions from the window's start to `now`, as runs with no cost whose project is (registry key,
    OWN), or (None, OWN) in no registered project's folder: those share a meter step by time like any run, and their
    part stays in the owner's use. Sessions of room workers (a folder or session id of a run in the ledger) are left
    out: they are runs already. Only for a provider whose sessions can be read, and only with projects."""
    if win.provider not in SESSION_PROVIDERS or not reg.projects:
        return []
    start = win.resets_at - pie.WEEK
    workers = [r["session_id"] for r in ledger.runs_during(win.provider, start, now) if r["session_id"]]
    found = sessions.spans({key: p.path for key, p in reg.projects.items()}, start, now,
                           exclude_dirs=ledger.worker_dirs(start), exclude_sessions=workers, projects_dir=transcripts)
    return [{"project": (span["project"], OWN), "started_at": span["started_at"], "ended_at": span["ended_at"],
             "cost_usd": None} for span in found]


def away_state(ledger, owner_share: float, win: Window, now: float, transcripts: Path | None = None) -> tuple[float | None, float]:
    """(the owner's stored return time while it is ahead and he is not at a session, the points of his slice lent while he is away). A stored value
    that can't be read, a return already past, or one of his own sessions active lends nothing."""
    try:
        until = away.load(away.path(Path(ledger.path).parent))
    except RegistryError:
        return None, 0.0
    if until is None or until <= now:
        return None, 0.0
    if away.owner_is_active(now, exclude=ledger.worker_dirs(now - 20 * 60), transcripts=transcripts):
        return None, 0.0   # a session of his own ends it at once
    return until, away.lent(owner_share, until, now, win.resets_at, False)


def standings(ledger, reg: Registry, provider: str, now: float, transcripts: Path | None = None) -> Report | None:
    """The registry's projects' standings on `provider` at `now`, or None when there is no current weekly window.
    Each project's spend is its room runs' and its own sessions' (read from `transcripts`, default Claude Code's)."""
    win = window(ledger, provider, now)
    if win is None:
        return None
    readings = samples(ledger, win, now)
    transfers = raises.current(raises.load(raises.path(Path(ledger.path).parent)), win, readings)
    shares, owner_share = raises.effective(reg, transfers)
    until, lent = away_state(ledger, owner_share, win, now, transcripts)
    shares, owner_share, lent = away.apply(shares, owner_share, lent)
    mine = own_runs(ledger, reg, win, now, transcripts)
    spent = pie.estimate_spend(readings, runs(ledger, win, now) + mine, now)
    own_last: dict = {}
    for r in mine:
        own_last[r["project"][0]] = max(own_last.get(r["project"][0], 0.0), r["ended_at"])
    names = ledger_names(reg)
    projects = {key: {"share": shares[key], "when": p.when, "strict": p.strict} for key, p in reg.projects.items()}
    own = {key: spent.get((key, OWN), 0.0) for key in names}
    by_key = {key: spent.get(name, 0.0) + own[key] for key, name in names.items()}
    slack = config_mod.DEFAULTS["budget"].get(provider, {}).get("weekly_slack", 0)
    rows = pie.standings(projects, by_key, win.elapsed, slack)
    known = set(names.values())
    unregistered = {name or NO_NAME: pct for name, pct in spent.items() if name not in known and not isinstance(name, tuple)}
    return Report(win, rows, owner_share, unregistered, own, transfers, raises.reset_at(readings), own_last, until, lent)
