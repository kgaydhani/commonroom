"""The pie: how much of the weekly allowance each project has spent, how much it may have spent by now, and whose turn
it is (commonroom#225).

Pure functions over plain data. No I/O, no clock, no ledger: every input is passed in, so each result can be checked by
hand. Nothing in the room calls this module yet. Percentages are percent of one provider's weekly window.
"""
from __future__ import annotations

from dataclasses import dataclass

WEEK = 7 * 86400


def elapsed_fraction(now: float, resets_at: float) -> float:
    """How much of the weekly window ending at `resets_at` has gone by at `now`, from 0 to 1."""
    return min(1.0, max(0.0, (now - (resets_at - WEEK)) / WEEK))


def _period(sample: dict) -> int:
    """Samples of one weekly window share a reset time; readings of it jitter, so round it to the hour."""
    return round((sample.get("resets_at") or 0) / 3600)


def _weights(active: list[tuple[dict, float]]) -> list[float]:
    """Each active run's part of one gap's growth. `active` holds (run, seconds of the run inside the gap)."""
    rates = []
    for run, _ in active:
        cost, end = run.get("cost_usd"), run.get("ended_at")
        if cost is None or end is None or end <= run["started_at"]:
            break
        rates.append(cost / (end - run["started_at"]))
    else:
        if sum(rates) > 0:
            return rates                                   # every run has a cost: by cost rate
    seconds = [overlap for _, overlap in active]
    if sum(seconds) > 0:
        return seconds                                     # a cost is missing: by minutes inside the gap
    return [1.0] * len(active)                             # a gap of no length: evenly


def estimate_spend(samples: list[dict], runs: list[dict]) -> dict:
    """An ESTIMATE of how much of the weekly window each project spent. The meter is plan-wide, so this is inferred.

    `samples` are one provider's weekly readings oldest first, shaped as `Ledger.usage_history` returns them
    (`first_seen`, `observed_at`, `used_pct`, `resets_at`). `runs` are every project's runs on that provider in the
    same span (`project`, `started_at`, `ended_at` or None while running, `cost_usd` or None).

    The reading moved somewhere between the last time the old value was seen (`observed_at`) and the first time the
    new one was (`first_seen`). That growth, clamped at zero, is shared among the runs active in that gap: by cost rate
    (`cost_usd` over the run's length) when every active run has a cost, otherwise by each run's minutes inside the
    gap. Growth with no run active is nobody's: it is the owner's own use. Two samples either side of a weekly reset
    belong to different periods, so a reset never counts as spend. Returns {project: percent}, projects that spent
    nothing left out.
    """
    spent: dict = {}
    ordered = sorted(runs, key=lambda r: (str(r["project"]), r["started_at"]))
    for prev, cur in zip(samples, samples[1:]):
        if _period(prev) != _period(cur):
            continue
        growth = max(0.0, cur["used_pct"] - prev["used_pct"])
        lo, hi = prev["observed_at"], cur["first_seen"]
        active = []
        for run in ordered:
            end = run["ended_at"] if run["ended_at"] is not None else float("inf")
            if run["started_at"] <= hi and end >= lo:
                active.append((run, max(0.0, min(end, hi) - max(run["started_at"], lo))))
        if not growth or not active:
            continue
        weights = _weights(active)
        total = sum(weights)
        for (run, _), weight in zip(active, weights):
            spent[run["project"]] = spent.get(run["project"], 0.0) + growth * weight / total
    return spent


@dataclass(frozen=True)
class Standing:
    name: str
    share: float          # percent of the weekly allowance
    spent: float          # percent of the weekly window spent so far (an estimate)
    allowed: float        # its line: what it may have spent by now
    when: str = "paced"
    strict: bool = False

    @property
    def headroom(self) -> float:
        return self.allowed - self.spent

    @property
    def behind(self) -> float:
        """Headroom relative to share: how far under its line a project is, for its size."""
        return self.headroom / self.share if self.share > 0 else float("-inf")


def line(share: float, elapsed: float, weekly_slack: float) -> float:
    """A project's line: `share * elapsed + share% of weekly_slack`."""
    return share * elapsed + weekly_slack * share / 100


def standings(projects: dict, spent: dict, elapsed: float, weekly_slack: float) -> list[Standing]:
    """One Standing per project, sorted by name. `projects` maps a name to {"share", "when", "strict"} (when and
    strict optional); `spent` comes from `estimate_spend`; `elapsed` from `elapsed_fraction`."""
    return [Standing(name, p["share"], spent.get(name, 0.0), line(p["share"], elapsed, weekly_slack),
                     p.get("when", "paced"), bool(p.get("strict", False)))
            for name, p in sorted(projects.items())]


def _first(group: list[Standing]) -> Standing:
    """The furthest behind for its size; equal standings go by name, so the answer never depends on input order."""
    return min(group, key=lambda s: (-s.behind, s.name))


def _lenders(stands: list[Standing], ready: set, plan_used: float, borrower: str) -> list[Standing]:
    """Projects lending their unused share to `borrower`: non-strict, under their line, with nothing ready. They lend
    only while the plan is under the sum of all the lines with strict projects' unused share held back."""
    held = sum(max(0.0, s.headroom) for s in stands if s.strict)
    if plan_used >= sum(s.allowed for s in stands) - held:
        return []
    return [s for s in stands if s.name != borrower and not s.strict and s.share > 0 and s.headroom > 0
            and s.name not in ready]


def whose_turn(stands: list[Standing], ready: set, plan_used: float) -> tuple[str | None, str]:
    """Which project goes next, and one sentence saying why (or None and why nobody may run).

    `ready` names the projects with work ready; `plan_used` is the plan-wide weekly reading. The rule, in order:
    1. A share of zero never runs.
    2. Among `paced` projects with work and headroom above zero, the one furthest behind for its size
       (headroom / share) goes.
    3. Otherwise a `paced` project with work that is over its line may run on lent share: another non-strict project
       is under its line with nothing ready, and the plan is under the sum of the lines (strict unused share held back).
    4. `spare` projects run only when no `paced` project could: on their own headroom, else on lent share.
    Ties go to the name that sorts first.
    """
    willing = [s for s in stands if s.name in ready and s.share > 0]
    if not willing:
        return None, "no project with a share above zero has work ready"
    for when in ("paced", "spare"):
        group = [s for s in willing if s.when == when]
        under = [s for s in group if s.headroom > 0]
        if under:
            s = _first(under)
            return s.name, (f"{s.name} ({when}) is furthest under its line: {s.spent:.1f}% spent of {s.allowed:.1f}% "
                            f"allowed, {s.headroom:.1f} points in hand on a share of {s.share:g}")
        for s in sorted(group, key=lambda s: (-s.behind, s.name)):
            lenders = _lenders(stands, ready, plan_used, s.name)
            if lenders:
                return s.name, (f"{s.name} ({when}) is over its line ({s.spent:.1f}% spent of {s.allowed:.1f}%) and runs "
                                f"on share lent by {', '.join(l.name for l in lenders)}, which have nothing ready")
    return None, ("every project with work is over its line, and no non-strict project under its line has unused share "
                  "to lend while the plan is under the sum of the lines")
