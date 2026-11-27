"""What a meter point costs, by model (commonroom#644).

Each UTC day, the points a Claude meter rose and the API-priced cost of that day's token rows (`tokens`), per model, give
one equation: points = sum over models of (points per dollar) x (dollars). Least squares over the days gives each model's
weight. One blended cost per point moves a quarter day to day as the mix changes; per-model weights follow the meter.
Reported as dollars per point, with a spread (leave-one-day-out) and the day-to-day spread of actual / predicted.

Days with an early reset (the meter fell before its reset time) are split out and never fitted across. A natural
rollover (the fall comes at the old reset time) counts the new window's first reading from zero. Today is left out (still
growing). With too little data the fit says what is missing and returns no number. Nothing here changes any budget.
Fits are kept in `weights-history.jsonl` in the room home, one line per day and meter, dated.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from commonroom import tokens

FILENAME = "weights-history.jsonl"
METERS = ("seven_day", "five_hour")
MIN_RISE = 3.0      # points a day must rise to be fitted
MIN_DAYS = 6        # usable days a fit needs, and at least models + 2
MODEL_DAYS = 3      # usable days a model must appear on to be fitted
ROLLOVER_SLACK = 1800.0
JITTER = 2.0        # points of disagreement between meter sources
FRESH = 2.0         # a window first seen at or below this many points began at zero

# API dollars per million tokens: input, output, cache write (5 minute), cache read. Read 25 Nov 2026 from the vendor's
# price page. A model not listed is priced as its family's latest.
PRICES = {"opus": (4.0, 20.0, 5.0, 0.20), "sonnet": (2.0, 10.0, 2.5, 0.10), "fable": (8.0, 40.0, 10.0, 0.20),
          "haiku": (0.1, 0.5, 0.125, 0.01)}
EXACT = {"claude-fable-5-1": (10.0, 50.0, 12.5, 0.25), "claude-opus-5": (5.0, 25.0, 6.25, 0.5)}


def history_file() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom") / FILENAME


def family(model: str) -> str | None:
    return next((f for f in PRICES if f in (model or "")), None)


def price(row: dict) -> tuple[str, float] | None:
    """(family, API dollars) of a token row, or None for a model with no price."""
    fam = family(row.get("model"))
    p = EXACT.get(row.get("model")) or PRICES.get(fam)
    if p is None:
        return None
    n = [int(row.get(f) or 0) for f in tokens.FIELDS]       # input, output, cache_write, cache_read
    return fam, sum(a * b for a, b in zip(n, p)) / 1e6


def daily_costs(rows: list[dict]) -> dict:
    """day -> {family: dollars}."""
    out: dict = {}
    for r in rows:
        priced = price(r)
        if priced and r.get("day"):
            d = out.setdefault(r["day"], {})
            d[priced[0]] = d.get(priced[0], 0.0) + priced[1]
    return out


def _day(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d")


def daily_rises(readings: list[dict]) -> tuple[dict, list[str]]:
    """(day -> points risen, days with an early reset) from a meter's readings (first_seen, used_pct, resets_at)."""
    rises: dict = {}
    resets: set = set()
    prev = None
    high = 0.0
    for r in sorted(readings, key=lambda r: (r["first_seen"], r.get("observed_at") or 0)):
        day, pct = _day(r["first_seen"]), r["used_pct"]
        if prev is None:
            high = pct
        elif window_changed(prev, r):
            if prev.get("resets_at") and r["first_seen"] >= prev["resets_at"] - ROLLOVER_SLACK and pct <= FRESH:
                rises[day] = rises.get(day, 0.0) + pct          # natural rollover: the new window began at zero
            else:
                resets.add(day)                                  # fell early, or first seen part-way: not fitted across
            high = pct
        else:
            rises[day] = rises.get(day, 0.0) + max(0.0, pct - high)
            high = max(high, pct)
        prev = r
    return rises, sorted(resets)


def window_changed(prev: dict, r: dict) -> bool:
    """Detect a meter reset from a drop beyond source jitter or a jump in its scheduled reset time."""
    a, b = prev.get("resets_at"), r.get("resets_at")
    return prev["used_pct"] - r["used_pct"] > JITTER or bool(a and b and abs(b - a) > ROLLOVER_SLACK)


def lsq(rows: list[list[float]], ys: list[float]) -> list[float]:
    """Least squares without an intercept (normal equations, Gaussian elimination). nan where it is singular."""
    k = len(rows[0])
    a = [[sum(r[i] * r[j] for r in rows) for j in range(k)] + [sum(r[i] * y for r, y in zip(rows, ys))] for i in range(k)]
    for i in range(k):
        piv = max(range(i, k), key=lambda r: abs(a[r][i]))
        a[i], a[piv] = a[piv], a[i]
        if abs(a[i][i]) < 1e-12:
            return [float("nan")] * k
        for r in range(k):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [x - f * y for x, y in zip(a[r], a[i])]
    return [a[i][k] / a[i][i] for i in range(k)]


def _dollars_per_point(w: float) -> float | None:
    return 1 / w if w and w > 0 and not math.isnan(w) else None


def utc_today() -> str:
    return _day(time.time())


def fit(rows: list[dict], readings: list[dict], meter: str, today: str | None = None) -> dict:
    """The fit for one meter: {"ok": True, "meter", "date", "days", "models": {family: {"dollars_per_point", "spread"}},
    "spread", "reset_days", "left_out"}, or {"ok": False, "meter", "missing": [...]} saying what is missing."""
    today = today or utc_today()
    rises, reset_days = daily_rises(readings)
    costs = daily_costs(rows)
    out = {"meter": meter, "reset_days": reset_days}
    days = sorted(d for d, p in rises.items() if p >= MIN_RISE and d < today and d not in reset_days and costs.get(d))
    missing = []
    if not readings:
        missing.append(f"no {meter} readings in the ledger")
    if not costs:
        missing.append("no token rows (run `room tokens collect`)")
    seen = {m: sum(1 for d in days if costs[d].get(m, 0) > 0) for m in {m for d in days for m in costs[d]}}
    models = sorted(m for m, n in seen.items() if n >= MODEL_DAYS)
    left_out = sorted(m for m in seen if m not in models)
    need = max(MIN_DAYS, len(models) + 2)
    if len(days) < need and readings and costs:
        missing.append(f"{len(days)} usable days of {need} needed (a day needs {MIN_RISE:g}+ points risen, token rows, no reset;"
                       f" {len(reset_days)} reset days split out)")
    if not models and readings and costs and days:
        missing.append(f"no model on {MODEL_DAYS}+ usable days")
    if missing:
        return {"ok": False, **out, "missing": missing}

    def solve(use):
        return lsq([[costs[d].get(m, 0.0) for m in models] for d in use], [rises[d] for d in use])
    w = solve(days)
    if any(math.isnan(x) for x in w):
        return {"ok": False, **out, "missing": ["the models' costs move together: they cannot be told apart yet"]}
    ratios = []
    for d in days:
        pred = sum(x * costs[d].get(m, 0.0) for x, m in zip(w, models))
        if pred > 0:
            ratios.append(rises[d] / pred)
    spread = statistics.pstdev(ratios) / statistics.mean(ratios) if len(ratios) > 1 and statistics.mean(ratios) else None
    loo = [solve([x for x in days if x != d]) for d in days]
    result = {}
    for i, m in enumerate(models):
        ests = [_dollars_per_point(s[i]) for s in loo if not math.isnan(s[i])]
        ests = [e for e in ests if e is not None]
        n = len(ests)
        sd = math.sqrt((n - 1) / n * sum((e - sum(ests) / n) ** 2 for e in ests)) if n > 1 else None
        result[m] = {"dollars_per_point": _dollars_per_point(w[i]), "spread": sd}
    return {"ok": True, **out, "date": today, "days": len(days), "models": result, "spread": spread, "left_out": left_out}


def read_history(path: Path | None = None) -> list[dict]:
    out = []
    try:
        text = (path or history_file()).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            row = json.loads(line)
            row["date"], row["meter"], row["models"]
            out.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    return out


def record(result: dict, path: Path | None = None) -> bool:
    """Keep a successful fit, dated: one line per date and meter (a second fit the same day replaces the first)."""
    if not result.get("ok"):
        return False
    path = path or history_file()
    keep = [r for r in read_history(path) if (r["date"], r["meter"]) != (result["date"], result["meter"])]
    keep.append({k: result[k] for k in ("date", "meter", "days", "models", "spread", "reset_days", "left_out")})
    keep.sort(key=lambda r: (r["date"], r["meter"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in keep), encoding="utf-8", newline="\n")
    return True


def _fmt(r: dict) -> str:
    parts = []
    for m, v in sorted(r["models"].items()):
        d, s = v["dollars_per_point"], v["spread"]
        parts.append(f"{m} " + ("n/a" if d is None else f"${d:.2f}") + ("" if s is None else f" ±{s:.2f}"))
    sp = r.get("spread")
    return f"{', '.join(parts)}; day-to-day spread {'n/a' if sp is None else f'{sp:.0%}'} over {r['days']} days"


def report(history: list[dict], failures: list[dict], limit: int = 14) -> list[str]:
    """The screen: the latest weights per meter, what is missing where there is no fit, then each meter's history."""
    lines = ["API dollars per meter point, by model (estimates; the meter is plan-wide)"]
    for meter in METERS:
        rows = [r for r in history if r["meter"] == meter]
        fail = next((f for f in failures if f["meter"] == meter), None)
        lines.append(f"{meter}:")
        if rows:
            lines.append(f"  latest {rows[-1]['date']}: {_fmt(rows[-1])}")
            if rows[-1].get("left_out"):
                lines.append(f"  left out (too few days): {', '.join(rows[-1]['left_out'])}")
        if fail:
            lines += [f"  not enough data: {x}" for x in fail["missing"]]
        elif not rows:
            lines.append("  no fits recorded yet")
        if len(rows) > 1:
            lines.append("  history:")
            lines += [f"    {r['date']}: {_fmt(r)}" for r in rows[-limit:][::-1]]
    return lines
