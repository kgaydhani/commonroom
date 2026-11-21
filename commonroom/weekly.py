"""Every weekly window a provider's meter reports, with a projection, and whether a move off one worked (commonroom#256).

A plan can have more than the all-models week (`seven_day`): Claude reports a smaller one for its top model as
`seven_day_<model>`, and Codex reports some as `seven_day:<limit>`. The window's name carries the model, so no model
name is written here. Read-only: `room pie` and the page show what this returns; nothing gates on it yet.

Readings are stored compressed: an unchanged reading only moves its row's `observed_at` on, so the value at a time is
the latest row first seen by then.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from commonroom import pie

ALL = "seven_day"
DAY = 86400
MIN_SPAN = 3600            # fewer seconds of readings than this give no rate
SAME_PERIOD = 3600         # readings of one weekly period carry one reset time give or take this


def is_weekly(window: str) -> bool:
    return window == ALL or window.startswith(ALL + "_") or window.startswith(ALL + ":")


def model_part(window: str) -> str | None:
    """The model a per-model window is for, as its name carries it (`seven_day_fable` -> `fable`); None for `seven_day`."""
    if window == ALL or not is_weekly(window):
        return None
    return window[len(ALL) + 1:] or None


def window_for(model: str | None, windows) -> str | None:
    """The per-model window among `windows` whose name carries `model`'s name, or None (only the all-models week)."""
    if not model:
        return None
    words = set(re.split(r"[\W_]+", model.lower()))
    for w in sorted(windows):
        part = model_part(w)
        if part and (part.lower() in words or part.lower() in model.lower()):
            return w
    return None


def value_at(rows: list[dict], t: float) -> dict | None:
    """The reading in effect at `t`: the latest row first seen by then."""
    found = None
    for r in sorted(rows, key=lambda r: (r["first_seen"], r["observed_at"])):
        if r["first_seen"] <= t:
            found = r
    return found


@dataclass(frozen=True)
class Projection:
    window: str
    used_pct: float | None        # None: seen this week but missing from the latest reading, so unknown (A5)
    resets_at: float | None
    observed_at: float | None
    rate_per_day: float | None    # points a day over the last 24 hours; None with too few readings
    runs_out_at: float | None     # when it reaches 100% at that rate, or None when it lasts the week

    @property
    def known(self) -> bool:
        return self.used_pct is not None

    @property
    def lasts(self) -> bool:
        return self.known and self.rate_per_day is not None and self.runs_out_at is None


def projection(window: str, rows: list[dict], resets_at: float, now: float) -> Projection:
    """At the rate of the last 24 hours (or since the period began, if later), when `window` reaches 100%.
    `rows` are its readings; those of another period (another reset time) are left out."""
    rows = [r for r in rows if r["resets_at"] is not None and abs(r["resets_at"] - resets_at) < SAME_PERIOD
            and r["first_seen"] <= now]
    cur = value_at(rows, now)
    if cur is None:
        return Projection(window, None, resets_at, None, None, None)
    start = max(now - DAY, resets_at - pie.WEEK)
    base = value_at(rows, start)
    if base is None:
        base = min(rows, key=lambda r: r["first_seen"])
        start = base["first_seen"]
    span = now - start
    if span < MIN_SPAN:
        return Projection(window, cur["used_pct"], resets_at, cur["observed_at"], None, None)
    rate = max(0.0, cur["used_pct"] - base["used_pct"]) / span
    out = now + (100 - cur["used_pct"]) / rate if rate > 0 else None
    return Projection(window, cur["used_pct"], resets_at, min(cur["observed_at"], now), rate * DAY,
                      out if out is not None and out < resets_at else None)


def windows(ledger, provider: str, now: float) -> list[Projection]:
    """Every current weekly window of `provider`, the all-models week first. A window seen in the last week, still
    in its period and missing from the latest reading is listed as unknown, never as free (axiom A5)."""
    latest = {w: r for w, r in ledger.latest_usage(provider).items() if is_weekly(w)}
    found = []
    for w, row in latest.items():
        if row["resets_at"] is not None and row["resets_at"] > now:
            found.append(projection(w, ledger.usage_history(provider, w, now - 2 * pie.WEEK), row["resets_at"], now))
    for w in ledger.windows_seen(provider, now - pie.WEEK):
        if is_weekly(w) and w not in latest:
            last = max(ledger.usage_history(provider, w, now - pie.WEEK), key=lambda r: r["observed_at"])
            if last["resets_at"] is not None and last["resets_at"] > now:
                found.append(Projection(w, None, last["resets_at"], last["observed_at"], None, None))
    return sorted(found, key=lambda p: (p.window != ALL, p.window))


@dataclass(frozen=True)
class Switch:
    window: str          # the window the work moved off
    at: float            # when it moved
    before: dict         # {window: used_pct} in effect at `at`
    after: dict          # {window: used_pct} at the latest reading since
    outcome: str         # "worked", "changed nothing", "nothing spent yet" or "no reading since"


def check_switch(histories: dict, window: str, at: float, now: float) -> Switch:
    """After work moved off `window` at `at`, compare the reading before with the latest since. The switch worked when
    `window` stopped rising while another weekly window rose; it changed nothing when `window` still rose.
    `histories` maps each weekly window to its readings."""
    before, after = {}, {}
    for w, rows in histories.items():
        b, a = value_at(rows, at), value_at(rows, now)
        if b is not None and a is not None:
            before[w], after[w] = b["used_pct"], a["used_pct"]
    seen_since = any(r["observed_at"] > at for r in histories.get(window, []))
    if window not in before or not seen_since:
        return Switch(window, at, before, after, "no reading since")
    if after[window] > before[window]:
        outcome = "changed nothing"
    elif any(after[w] > before[w] for w in after if w != window):
        outcome = "worked"
    else:
        outcome = "nothing spent yet"
    return Switch(window, at, before, after, outcome)


def room_switch(runs: list[dict], names) -> tuple[str, float] | None:
    """The room's latest move off a per-model window: (window, when), from its runs (oldest first, with `model`,
    `started_at`, `ended_at`). It moved when its last run on that window has ended and a run on another model started
    after. None when the room never moved, or still has a run on the window."""
    latest = None
    for w in sorted(n for n in names if model_part(n)):
        on = [r for r in runs if window_for(r.get("model"), [w]) == w]
        if not on or any(r["ended_at"] is None for r in on):
            continue
        last_end = max(r["ended_at"] for r in on)
        if any(r["started_at"] >= last_end and r.get("model") and window_for(r["model"], [w]) is None for r in runs):
            if latest is None or last_end > latest[1]:
                latest = (w, last_end)
    return latest


def switch(ledger, provider: str, now: float, found: list[Projection]) -> Switch | None:
    """Whether the room's latest move off a per-model window (this week) worked; None with one weekly window only."""
    names = [p.window for p in found if p.known]
    if len(names) < 2:
        return None
    moved = room_switch([r for r in ledger.runs_during(provider, now - pie.WEEK, now) if r["model"]], names)
    if moved is None:
        return None
    return check_switch({w: ledger.usage_history(provider, w, now - 2 * pie.WEEK) for w in names}, moved[0], moved[1], now)
