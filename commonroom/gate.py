"""Decide whether autonomous work may spend a provider's allowance right now. Pure functions, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field

from commonroom.usage import WINDOW_SECONDS

REQUIRED = ("five_hour", "seven_day")
STALE_MAX_AGE = 3600   # never act on a reading older than this
STALE_HEADROOM = 30    # percentage points of headroom needed to act on a stale reading
MIN_START_HEADROOM = 5 # never start a job on the last few points of a window: it would be refused mid-run


@dataclass
class Verdict:
    allowed: bool
    reason: str
    detail: dict = field(default_factory=dict)


def _fraction(now: float, resets_at: float, window: str) -> float:
    length = WINDOW_SECONDS[window]
    return min(1.0, max(0.0, (now - (resets_at - length)) / length))


def usage_verdict(provider: str, readings: dict[str, dict], policy: dict, now: float, freshness: float) -> Verdict:
    """`readings` maps window name to the latest ledger row for that window."""
    detail = {}
    stale_note = ""
    for window in REQUIRED:
        row = readings.get(window)
        if row is None:
            return Verdict(False, f"{provider}: no {window} reading yet", detail)
        if row.get("resets_at") and row["resets_at"] <= now:
            return Verdict(False, f"{provider}: {window} has reset since the last reading; waiting for a fresh one", detail)
        age = now - row["observed_at"]
        if age > freshness:
            # A stale reading far from every limit is still safe to act on: nothing burns 30 points unseen.
            reserve = policy["reserve_5h"] if window == "five_hour" else policy["reserve_weekly"]
            headroom = (100 - reserve) - row["used_pct"]
            if age > STALE_MAX_AGE or headroom < STALE_HEADROOM:
                return Verdict(False, f"{provider}: {window} reading is {int(age // 60)} min old and too close to its limit to trust", detail)
            stale_note = f" (reading {int(age // 60)} min old, but far from limits)"

    week, session = readings["seven_day"], readings["five_hour"]
    reserve_w, reserve_5 = policy["reserve_weekly"], policy["reserve_5h"]
    detail.update(weekly_used=week["used_pct"], five_hour_used=session["used_pct"],
                  weekly_resets_at=week.get("resets_at"), five_hour_resets_at=session.get("resets_at"))

    if 100 - week["used_pct"] <= reserve_w:
        return Verdict(False, f"{provider}: weekly reserve reached ({week['used_pct']:.0f}% used, {reserve_w}% kept back)", detail)
    if 100 - session["used_pct"] <= reserve_5:
        return Verdict(False, f"{provider}: 5-hour reserve reached ({session['used_pct']:.0f}% used, {reserve_5}% kept back)", detail)
    for name, row in (("weekly", week), ("5-hour", session)):
        # Even when running a provider dry, a job started on the last few points fails at once.
        if 100 - row["used_pct"] < MIN_START_HEADROOM:
            return Verdict(False, f"{provider}: {name} window nearly empty ({row['used_pct']:.0f}% used)", detail)

    if policy.get("pace") and week.get("resets_at") and session.get("resets_at"):
        weekly_allowed = (100 - reserve_w) * _fraction(now, week["resets_at"], "seven_day") + policy.get("weekly_slack", 0)
        burst_allowed = (100 - reserve_5) * min(1.0, _fraction(now, session["resets_at"], "five_hour") + policy.get("front_load", 0))
        detail.update(weekly_pace=round(weekly_allowed, 1), five_hour_cap=round(burst_allowed, 1))
        if week["used_pct"] > weekly_allowed:
            return Verdict(False, f"{provider}: ahead of weekly pace ({week['used_pct']:.0f}% used, pace allows {weekly_allowed:.0f}%)", detail)
        if session["used_pct"] > burst_allowed:
            return Verdict(False, f"{provider}: 5-hour burn cap ({session['used_pct']:.0f}% used, {burst_allowed:.0f}% allowed so far this window)", detail)

    return Verdict(True, f"{provider}: allowed{stale_note}", detail)


def breaker_verdict(open_breakers: list[dict], recent_finished_autonomous: list[dict], no_output_runs: int) -> tuple[Verdict, dict | None]:
    """Returns the verdict and, if a new breaker should trip, its record."""
    if open_breakers:
        b = open_breakers[0]
        return Verdict(False, f"breaker open: {b['kind']} - {b['detail']}"), None
    window = recent_finished_autonomous[:no_output_runs]
    if no_output_runs and len(window) == no_output_runs and all(not r["outputs"] for r in window):
        detail = (f"last {no_output_runs} autonomous runs produced nothing: "
                  + ", ".join(f"{r['role']}/{r['task'] or '-'}:{r['status']}" for r in window))
        return Verdict(False, "breaker tripped: " + detail), {"kind": "no_output", "detail": detail}
    return Verdict(True, "no breakers"), None
