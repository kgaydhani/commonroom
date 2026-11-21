"""Tell the owner when one of his own sessions spends a per-model weekly window faster than the week allows
(commonroom#256, part 4).

A plan's top model can have its own, smaller weekly window (`seven_day_<model>`). When that window runs out before
its reset at the rate of the last 24 hours, and a session of the owner's in this project's folder has used that model
in the last minutes, the owner is told once per session per window period: its folder, its model, its context size,
and what the same work would spend on the model the room's own runs use without such a window. His session is never
changed (axioms A0 and A5). The text is commonroom/ways/pie/model-window.md (axiom A1). Plain code, no model, and no
model name: the window's name and the transcripts carry them."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from commonroom import pie_notice, weekly

TEMPLATE = Path(__file__).resolve().parent / "ways" / "pie" / "model-window.md"


def hot(found: list[weekly.Projection]) -> list[weekly.Projection]:
    """The per-model windows that run out before their reset at the last day's rate."""
    return [p for p in found if weekly.model_part(p.window) and p.known and p.runs_out_at is not None]


def next_model(runs: list[dict], windows) -> str | None:
    """The model the room's runs used most this week that has no weekly window of its own, or None."""
    counts: dict = {}
    for r in runs:
        model = r.get("model")
        if model and weekly.window_for(model, windows) is None:
            counts[model] = counts.get(model, 0) + 1
    return max(sorted(counts), key=counts.get) if counts else None


def due(spans: list[dict], key: str, found: list[weekly.Projection], now: float,
        minutes: float = pie_notice.ACTIVE_MINUTES) -> list[tuple[dict, str, weekly.Projection]]:
    """(span, model, window) for each of the owner's sessions in project `key` that used a model whose own window is
    running out, in its last `minutes`. One per session and window: its latest span."""
    names = [p.window for p in found]
    picked: dict = {}
    for p in hot(found):
        for span in spans:
            if span["project"] != key or span["ended_at"] < now - minutes * 60:
                continue
            on = [m for m in span.get("models", {}) if m and weekly.window_for(m, names) == p.window]
            if on:
                model = max(on, key=lambda m: span["models"][m])
                picked[(span["session_id"], p.window)] = (span, model, p)
    return [picked[k] for k in sorted(picked)]


def mark_key(provider: str, session: str, p: weekly.Projection) -> str:
    return f"model-notice:{provider}:{p.window}:{round((p.resets_at or 0) / 3600)}:{session}"


def _at(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


def text(span: dict, model: str, p: weekly.Projection, found: list[weekly.Projection], other: str | None) -> str:
    week = next((w for w in found if w.window == weekly.ALL and w.known), None)
    values = {"folder": span.get("folder") or "?", "model": model, "window": p.window, "used": f"{p.used_pct:g}",
              "runs_out": _at(p.runs_out_at), "resets": _at(p.resets_at), "context": f"{span.get('context', 0):,}",
              "other": other or "a model without its own weekly window",
              "week": f"{week.used_pct:g}" if week else "?"}
    body = TEMPLATE.read_text(encoding="utf-8-sig").rstrip("\n")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], body)
