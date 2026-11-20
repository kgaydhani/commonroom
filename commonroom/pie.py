"""The pie: how much of the weekly allowance each project has spent, how much it may have spent by now, and whose turn
it is (commonroom#225).

Pure functions over plain data. No I/O, no clock, no ledger: every input is passed in, so each result can be checked by
hand. Nothing in the room calls this module yet. Percentages are percent of one provider's weekly window.
"""
from __future__ import annotations

from dataclasses import dataclass

WEEK = 7 * 86400
# The weekly meter flaps by a point (two readers interleave 16, 17, 16, 17). A fall of more than this inside one period
# is not flapping but an early reset of the window (52, then 3), so the running high starts again from that reading.
RESET_DROP = 5


def elapsed_fraction(now: float, resets_at: float) -> float:
    """How much of the weekly window ending at `resets_at` has gone by at `now`, from 0 to 1."""
    return min(1.0, max(0.0, (now - (resets_at - WEEK)) / WEEK))


def _period(sample: dict) -> int:
    """Samples of one weekly window share a reset time; readings of it jitter, so round it to the hour."""
    return round((sample.get("resets_at") or 0) / 3600)


def _active(runs: list[dict], lo: float, hi: float, until: float) -> list[tuple[dict, float]]:
    """(run, seconds it overlaps the gap lo..hi) for each run inside the gap. A run still open counts up to `until`.
    A gap of no length (the two readings carry one timestamp) is an instant: each run spanning it counts one second."""
    found = []
    for run in runs:
        start = run["started_at"]
        end = run["ended_at"] if run["ended_at"] is not None else until
        if hi > lo:
            seconds = min(end, hi) - max(start, lo)
        else:
            seconds = 1.0 if start <= hi <= end else 0.0
        if seconds > 0:
            found.append((run, seconds))
    return found


def _weights(active: list[tuple[dict, float]]) -> list[float]:
    """Each active run's part of one gap's growth: its expected cost inside the gap (cost rate x seconds of overlap)
    when every active run has a cost, otherwise its seconds of overlap."""
    costs = []
    for run, seconds in active:
        cost, start, end = run.get("cost_usd"), run["started_at"], run.get("ended_at")
        if cost is None or end is None or end <= start:
            break
        costs.append(cost / (end - start) * seconds)
    else:
        if sum(costs) > 0:
            return costs
    return [seconds for _, seconds in active]


def estimate_spend(samples: list[dict], runs: list[dict], until: float) -> dict:
    """An ESTIMATE of how much of the weekly window each project spent. The meter is plan-wide, so this is inferred.

    `samples` are one provider's weekly readings, shaped as `Ledger.usage_history` returns them (`first_seen`,
    `observed_at`, `used_pct`, `resets_at`); they are put in order by `first_seen`, then `observed_at`, then highest
    reading first, so readings that share a timestamp come out the same whatever order they arrive in. `runs` are every
    project's runs on that provider in the same span (`project`, `started_at`, `ended_at` or None, `cost_usd` or None).
    A run with no end counts as running up to `until` (normally now). The caller must leave out runs whose dispatcher
    died, or they count as active until `until`.

    Growth counts only above the highest reading so far in the weekly period, so a meter that flaps (16, 17, 16, 17,
    18) counts 2, not 3. A fall of more than RESET_DROP points is an early reset of the window, and the high starts
    again from there. A reset between periods never counts. Each counted step happened between the last time the
    previous reading was seen (`observed_at`) and the first time the new one was (`first_seen`), and is shared among
    the runs active in that gap by their expected cost in it (cost rate x seconds of overlap) when every one has a cost,
    otherwise by seconds of overlap. Growth with no run active is nobody's: it is the owner's own use. So the projects
    together never get more than the period grew. Returns {project: percent}, projects that spent nothing left out.
    """
    spent: dict = {}
    ordered_runs = sorted(runs, key=lambda r: (str(r["project"]), r["started_at"]))
    ordered = sorted(samples, key=lambda s: (s["first_seen"], s["observed_at"], -s["used_pct"]))
    high: dict = {}
    last: dict = {}
    for cur in ordered:
        key, used = _period(cur), cur["used_pct"]
        prev = last.get(key)
        last[key] = cur
        if prev is None or used < high[key] - RESET_DROP:
            high[key] = used                       # a new period, or an early reset inside one
            continue
        if used <= high[key]:
            continue                               # flapping below the high: nothing new
        growth, high[key] = used - high[key], used
        hi = cur["first_seen"]
        lo = min(prev["observed_at"], hi)
        active = _active(ordered_runs, lo, hi, until)
        if not active:
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


def _lenders(stands: list[Standing], ready: set, borrower: str) -> list[Standing]:
    """Projects lending their unused share to `borrower`: non-strict, under their line, with nothing ready. They lend
    only while the projects' spend in total is under the sum of their lines with strict projects' unused share held
    back. The owner's own use doesn't enter: his slice and the plan-wide limit are the gate's business."""
    held = sum(max(0.0, s.headroom) for s in stands if s.strict)
    if sum(s.spent for s in stands) >= sum(s.allowed for s in stands) - held:
        return []
    return [s for s in stands if s.name != borrower and not s.strict and s.share > 0 and s.headroom > 0
            and s.name not in ready]


def whose_turn(stands: list[Standing], ready: set) -> tuple[str | None, str]:
    """Which project goes next, and one sentence saying why (or None and why nobody may run).

    `ready` names the projects with work ready. The rule, in order:
    1. A share of zero never runs.
    2. Among `paced` projects with work and headroom above zero, the one furthest behind for its size
       (headroom / share) goes.
    3. Otherwise a `paced` project with work that is over its line may run on lent share: another non-strict project
       is under its line with nothing ready, and the projects' spend in total is under the sum of their lines with
       strict projects' unused share held back.
    4. `spare` projects run only when no `paced` project could: on their own headroom, else on lent share.
    Ties go to the name that sorts first. The owner's slice and the plan-wide limits are the gate's, not this rule's.
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
            lenders = _lenders(stands, ready, s.name)
            if lenders:
                return s.name, (f"{s.name} ({when}) is over its line ({s.spent:.1f}% spent of {s.allowed:.1f}%) and runs "
                                f"on share lent by {', '.join(l.name for l in lenders)}, which have nothing ready")
    return None, ("every project with work is over its line, and no non-strict project under its line has unused share "
                  "to lend while the projects' spend is under the sum of their lines")
