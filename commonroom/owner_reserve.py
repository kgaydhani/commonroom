"""The gate's settings for a project in the pie's registry (commonroom#241): its weekly reserve is the owner's unused
slice.

The owner's slice is what the shares leave (`Registry.owner_share`). Under the pie only the part of it he hasn't used
needs holding back, and never less than the room's `owner_floor_weekly`, so the projects can't run the week out once he
has used his slice (axiom A5). Each project's own line is its weekly pace, so the plan-wide weekly pace check is off.
A registered project uses its own gate settings and breakers, never a `[share] host`'s. Everything else in the gate
stays as it is.

A project the registry doesn't list, and every project while it lists none or can't be read, is gated exactly as
before: `for_project` returns None. Standings come from `commonroom.standings`; they are ESTIMATES.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from commonroom import policy as policy_mod, registry as registry_mod, sessions, standings as standings_mod
from commonroom.registry import Registry

FLOOR = policy_mod.DEFAULTS["owner_floor_weekly"]
# {(ledger path, provider): (what the estimate was made from, (owner's slice, his own use) or None)}. The standings
# read every transcript of the week, which takes seconds, and the gate is asked on every line a worker prints, so the
# estimate is made again only when the weekly meter or the registry changes.
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
    key: str            # the project's registry key
    reg: Registry
    cfg: dict           # the project's config with its own gate settings, and no host

    def policy(self, ledger, provider: str, base: dict, now: float, transcripts: Path | None = None) -> tuple[dict, dict]:
        """The policy for `gate.usage_verdict` on `provider`, and what it was made from (for the verdict's detail).
        With no current weekly window, or standings that can't be made, the weekly reserve and pace are the project's
        own `[budget]`: an unknown slice is not free capacity."""
        policy = dict(base)
        if self.reg.budget.get("reserve_5h") is not None:
            policy["reserve_5h"] = self.reg.budget["reserve_5h"]
        try:
            found = _owner(ledger, self.reg, provider, now, transcripts)
        except Exception as exc:   # an estimate that can't be made never lowers the reserve
            return policy, {"unknown": f"the pie's standings can't be computed ({exc}); this project's own [budget] holds"}
        if found is None:
            return policy, {"unknown": "no current weekly reading for the pie; this project's own [budget] holds"}
        owner_share, owner_use = found
        policy.update(reserve_weekly=reserve(owner_share, owner_use), weekly_pace_check=False)
        return policy, {"owner_share": owner_share, "owner_use": round(owner_use, 1), "floor": FLOOR,
                        "reserve_weekly": policy["reserve_weekly"]}


def for_project(ledger, cfg: dict) -> OwnerGate | None:
    """The owner gate for the project `cfg` describes, when the registry beside the ledger lists its folder. None, so
    the gate is exactly today's, when it isn't listed, the registry is missing or empty, or it can't be read."""
    try:
        reg = registry_mod.load(Path(ledger.path).parent / registry_mod.FILENAME)
    except (registry_mod.RegistryError, OSError):
        return None
    key = key_for(reg, cfg.get("_root"))
    if key is None:
        return None
    own = {**cfg, **(cfg.get("_own") or {})}
    own.pop("_host", None)
    own.pop("_own", None)
    return OwnerGate(key, reg, own)


def _owner(ledger, reg: Registry, provider: str, now: float, transcripts: Path | None) -> tuple[float, float] | None:
    """(the owner's slice, his estimated own use) on `provider`, or None with no current weekly window."""
    latest = ledger.latest_usage(provider).get(standings_mod.WINDOW)
    made_from = (registry_mod.dumps(reg), latest and (latest["used_pct"], latest["resets_at"]))
    cached = _CACHE.get((str(ledger.path), provider))
    if cached and cached[0] == made_from:
        return cached[1]
    report = standings_mod.standings(ledger, reg, provider, now, transcripts)
    found = None if report is None else (report.owner_share, report.owner_use)
    _CACHE[(str(ledger.path), provider)] = (made_from, found)
    return found
