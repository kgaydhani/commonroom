"""Tell the owner when his own session takes a project over its share (commonroom#243).

His own sessions are never blocked (axioms A0 and A5): the room only says so, and makes yes one command. Plain code, no
model. The text is commonroom/ways/pie/over-share.md (axiom A1). `over` and `text` serve the phone notice, the status
line and `room pie`; `due` and `send` decide and record the phone notice, once per project per weekly window."""
from __future__ import annotations

import math
import re
from pathlib import Path

from commonroom import policy, raises, registry, standings
from commonroom.raises import OWNER

TEMPLATE = Path(__file__).resolve().parent / "ways" / "pie" / "over-share.md"
MARGIN = policy.DEFAULTS["pie_notice_margin"]
ACTIVE_MINUTES = policy.DEFAULTS["pie_own_active_minutes"]


def over(report: standings.Report, key: str, margin: float = 0.0):
    """The project's row when its estimated spend is at least `margin` points past its line, else None."""
    row = next((s for s in report.rows if s.name == key), None)
    return row if row is not None and row.share > 0 and row.spent - row.allowed >= max(margin, 1e-9) else None


def needed(report: standings.Report, row) -> float:
    """Points that cover the rest of the week at the current rate: the week's projected spend less its share, in tenths."""
    projected = row.spent / report.window.elapsed if report.window.elapsed > 0 else row.spent
    return max(0.1, math.ceil((projected - row.share) * 10) / 10)


def text(reg, report: standings.Report, key: str, row) -> str:
    """The notice: the standing, the command that raises the line and what it takes from each other project."""
    points = needed(report, row)
    command = f"room pie raise {key} {points:g}"
    try:
        entry = raises.transfer(reg, report, report.raises, key, points, None)
        names = {OWNER: "your own slice"}
        cost = "That takes " + ", ".join(f"{v:.1f} from {names.get(k, k)}" for k, v in entry["from"].items()) + "."
    except registry.RegistryError as exc:
        cost = f"That can't be done as it is: {exc}."
    values = {"project": key, "provider": report.window.provider, "spent": f"{row.spent:.1f}", "line": f"{row.allowed:.1f}",
              "share": registry.fmt_number(row.share), "command": command, "cost": cost}
    body = TEMPLATE.read_text(encoding="utf-8-sig").rstrip("\n")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], body)


def own_active(report: standings.Report, key: str, now: float, minutes: float = ACTIVE_MINUTES) -> bool:
    return report.own_last.get(key, 0.0) >= now - minutes * 60


def _base(report: standings.Report, key: str) -> str:
    w = report.window
    return f"pie-notice:{w.provider}:{key}:{round(w.resets_at / 3600)}:{int(report.reset_at)}"


def due(ledger, project, report: standings.Report, key: str, now: float, margin: float = MARGIN,
        minutes: float = ACTIVE_MINUTES) -> bool:
    """Whether the owner is to be told now. Re-arms (and so tells again) only after the project has been `margin`
    points below its line since the last notice. A raise changes the share, which is part of the mark's key, so a used
    up raise tells again. Records the dip below the line."""
    row = next((s for s in report.rows if s.name == key), None)
    if row is None:
        return False
    base = f"{_base(report, key)}:{registry.fmt_number(row.share)}"
    if row.allowed - row.spent >= margin:
        ledger.mark(project, f"{base}:below", now)
    if over(report, key, margin) is None or not own_active(report, key, now, minutes):
        return False
    sent, below = ledger.marked(project, f"{base}:sent"), ledger.marked(project, f"{base}:below")
    return sent is None or (below is not None and below > sent)


def sent(ledger, project, report: standings.Report, key: str, now: float) -> None:
    row = next(s for s in report.rows if s.name == key)
    ledger.mark(project, f"{_base(report, key)}:{registry.fmt_number(row.share)}:sent", now)
