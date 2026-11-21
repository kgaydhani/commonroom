"""The gate's settings for a project in the pie's registry (commonroom#241): its weekly reserve is the owner's unused
slice.

The owner's slice is what the shares leave (`Registry.owner_share`). Under the pie only the part of it he hasn't used
needs holding back, and never less than the room's `owner_floor_weekly`, so the projects can't run the week out once he
has used his slice (axiom A5). Each project's own line is its weekly pace (the pie's gate, commonroom#240), so the
plan-wide weekly pace check is off. A registered project uses its own gate settings and breakers, never a
`[share] host`'s, except its 5-hour reserve, which stays what it is today unless the registry or its own room.toml
sets one. Everything else in the gate stays as it is.

A project the registry doesn't list, and every project while it lists none, is gated exactly as before: `for_project`
returns None. When the pie can't be read (a registry that can't be loaded, no current weekly reading, standings that
raise) the gate is today's too, with a note saying so. Standings come from `commonroom.standings`; they are ESTIMATES.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from commonroom import config as config_mod, policy as policy_mod, raises, registry as registry_mod, sessions
from commonroom import standings as standings_mod
from commonroom.registry import Registry

FLOOR = policy_mod.DEFAULTS["owner_floor_weekly"]
# {(ledger path, provider): (what the estimate was made from, (owner's slice, his own use) or None, error or None)}.
# The standings read every transcript of the week, which takes seconds, and the gate is asked on every line a worker
# prints, so the estimate (or its failure) is made again only when the meter, registry or weekly raises change.
_CACHE: dict = {}


def reserve(owner_share: float, owner_use: float, floor: float = FLOOR) -> float:
    """The weekly reserve: the owner's unused slice, `owner_share - owner_use`, and never less than `floor`. Rounded to
    a tenth of a point, and whole numbers as int, so the gate's reason reads `5% kept back`."""
    value = round(max(owner_share - owner_use, floor), 1)
    return int(value) if value == int(value) else value


def key_for(reg: Registry, root) -> str | None:
    """The registry key whose folder is `root`, or None."""
    if not root:
        return None
    want = sessions.path_key(root)
    return next((k for k, p in reg.projects.items() if isinstance(p.path, str) and sessions.path_key(p.path) == want),
                None)


@dataclass
class OwnerGate:
    key: str | None              # the project's registry key; None when the registry can't be read
    reg: Registry | None
    cfg: dict                    # the project's config with its own gate settings, and no host
    today: dict                  # the config as passed in: today's gate, a host's settings and breakers included
    problem: str | None = None   # why the pie can't be read at all

    def gate(self, ledger, provider: str, now: float,
             transcripts: Path | None = None) -> tuple[dict, dict, dict, str | None]:
        """(config, policy for `gate.usage_verdict`, what it was made from, note) on `provider`. When the pie can't be
        read, today's config and policy exactly, with a note saying so: an unknown share is never free allowance."""
        problem, found = self.problem, None
        if problem is None:
            try:
                found = _owner(ledger, self.reg, provider, now, transcripts)
            except Exception as exc:
                problem = f"its standings can't be computed: {exc}"
            else:
                problem = None if found else "it has no current weekly reading"
        if problem:
            note = f"the pie can't be read ({problem}), so today's gate applies"
            return self.today, config_mod.budget(self.today, provider), {"unknown": note}, note
        owner_share, owner_use = found
        policy = dict(config_mod.budget(self.cfg, provider), weekly_pace_check=False,
                      reserve_weekly=reserve(owner_share, owner_use), reserve_5h=self.reserve_5h(provider))
        return self.cfg, policy, {"owner_share": owner_share, "owner_use": round(owner_use, 1), "floor": FLOOR,
                                  "reserve_weekly": policy["reserve_weekly"]}, None

    def reserve_5h(self, provider: str):
        """The registry's `[budget] reserve_5h`, else the one the project's own room.toml sets, else today's (a host's
        included): only the weekly reserve and pace change for a registered project."""
        if self.reg.budget.get("reserve_5h") is not None:
            return self.reg.budget["reserve_5h"]
        own = (self.today.get("_own_budget") or {}).get(provider) or {}
        if own.get("reserve_5h") is not None:
            return own["reserve_5h"]
        return config_mod.budget(self.today, provider)["reserve_5h"]


def for_project(ledger, cfg: dict) -> OwnerGate | None:
    """The owner gate for the project `cfg` describes, when the registry beside the ledger lists its folder, or breaks a
    rule and may list it. None, so the gate is exactly today's, when the registry is missing, empty, or doesn't list
    it."""
    path = Path(ledger.path).parent / registry_mod.FILENAME
    try:
        reg = registry_mod.load(path)
    except (registry_mod.RegistryError, OSError) as exc:
        try:   # read without its rules: a registry that plainly doesn't list this project changes nothing for it
            if key_for(registry_mod.load(path, check=False), cfg.get("_root")) is None:
                return None
        except (registry_mod.RegistryError, OSError, TypeError, ValueError):
            pass
        return OwnerGate(None, None, cfg, cfg, f"the project registry can't be read: {exc}")
    key = key_for(reg, cfg.get("_root"))
    if key is None:
        return None
    own = {**cfg, **(cfg.get("_own") or {})}
    for private in ("_host", "_own", "_own_budget"):
        own.pop(private, None)
    return OwnerGate(key, reg, own, cfg)


def _owner(ledger, reg: Registry, provider: str, now: float, transcripts: Path | None) -> tuple[float, float] | None:
    """(the owner's slice, his estimated own use) on `provider`, or None with no current weekly window. A failure is
    remembered like a result, so it isn't retried on every line a worker prints."""
    latest = ledger.latest_usage(provider).get(standings_mod.WINDOW)
    try:
        transfers = raises.path(Path(ledger.path).parent).stat()
        stamp = (transfers.st_mtime_ns, transfers.st_size)
    except FileNotFoundError:
        stamp = None
    made_from = (registry_mod.dumps(reg), latest and (latest["used_pct"], latest["resets_at"], now < (latest["resets_at"] or 0)), stamp)
    cached = _CACHE.get((str(ledger.path), provider))
    if not (cached and cached[0] == made_from):
        try:
            report = standings_mod.standings(ledger, reg, provider, now, transcripts)
            cached = (made_from, None if report is None else (report.owner_share, report.owner_use), None)
        except Exception as exc:
            cached = (made_from, None, exc)
        _CACHE[(str(ledger.path), provider)] = cached
    if cached[2] is not None:
        raise cached[2]
    return cached[1]
