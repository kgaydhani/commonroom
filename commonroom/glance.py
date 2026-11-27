"""The page's layer section (commonroom#585, docs/layer.md conclusion 8): what the layer did today. Each figure carries
`how` (how it is known) and `leaves_out` (what it does not count), so the owner can tell a zero from a blind spot.
Reads only: the ledger's routes, the fresh-start record in the room home, and the person file."""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import date, datetime

from commonroom import person, places

OFFERS = "session-offers.json"   # written by the prompt hook (commonroom#580): {session: {"at": time}}


def day_start(now: float) -> float:
    """Local midnight before `now`."""
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _routes(ledger, since: float) -> dict:
    try:
        rows = [r for r in ledger.routes() if r["at"] >= since]
    except Exception:
        rows = None
    base = {"how": "Counted from the ledger's routes table, one row each time a session ran `room route`, or `room note` or "
                   "`room hand` into another place.",
            "leaves_out": "Anything a session did itself or sent some other way, and routes made before midnight."}
    if rows is None:
        return base | {"error": "the ledger's routes could not be read", "total": None, "to": [], "undone": 0}
    live = [r for r in rows if not r.get("undone_at")]
    to = Counter((r["to_place"], r["how"]) for r in live)
    return base | {"total": len(live), "undone": len(rows) - len(live),
                   "to": [{"place": p, "how": h, "count": n} for (p, h), n in sorted(to.items(), key=lambda x: (-x[1], x[0]))]}


def _offers(since: float) -> dict:
    base = {"how": "Counted from the record the prompt hook keeps in the room home, one entry per session offered a fresh start.",
            "leaves_out": "Whether the session took the offer: that is not recorded yet. Offers made before midnight."}
    try:
        seen = json.loads(places.path_file().with_name(OFFERS).read_text(encoding="utf-8"))
    except FileNotFoundError:
        seen = {}
    except (OSError, ValueError):
        return base | {"error": "the offer record could not be read", "offered": None, "took": None}
    if not isinstance(seen, dict):
        return base | {"error": "the offer record could not be read", "offered": None, "took": None}
    n = sum(1 for v in seen.values() if isinstance(v, dict) and isinstance(v.get("at"), (int, float)) and v["at"] >= since)
    return base | {"offered": n, "took": None}   # None: not yet recorded


def _person(now: float) -> dict:
    base = {"how": "Read from person.md in the room home, now: its lines against the cap in policy, tokens estimated as "
                   "characters / 4, the oldest line by the date it starts with.",
            "leaves_out": "What the vendors' own instruction files hold, and whether any session loaded the file."}
    try:
        lines = person.load()
    except OSError:
        return base | {"error": "the person file could not be read", "lines": None}
    dates = []
    for x in lines:
        try:
            dates.append(date.fromisoformat(x[:10]))
        except ValueError:
            pass
    oldest = min(dates) if dates else None
    return base | {"lines": len(lines), "max_lines": person.MAX_LINES, "tokens": person.tokens(lines),
                   "max_tokens": person.MAX_TOKENS, "oldest": oldest.isoformat() if oldest else None,
                   "oldest_days": (date.fromtimestamp(now) - oldest).days if oldest else None}


def layer(ledger, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    since = day_start(now)
    return {"since": since, "routes": _routes(ledger, since), "offers": _offers(since), "person": _person(now)}
