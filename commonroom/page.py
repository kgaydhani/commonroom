"""What the dashboard's one page shows (commonroom#248): the week, the pie, what waits on the owner, and each project.

`page_state` is given what it reads (the ledger, the config, the registry, `now`, and a function that lists the owner's
tasks for a repo), so a test can run it on a temporary room home with no network and no clock. Every spend figure is an
estimate, because the meter is plan-wide (see `commonroom.pie`); the page marks them so.
"""
from __future__ import annotations

from pathlib import Path

from commonroom import board, config as config_mod, handshake, limits, metrics, pie, registry as registry_mod, standings
from commonroom.dispatch import pie_active, pie_holds, verdicts
from commonroom.liveness import orphaned
from commonroom.localboard import LocalBoard

PROVIDER = "claude"
WEEK_SECONDS = pie.WEEK
RECENT_LANDINGS = 3


def _state_word(row: pie.Standing, ok: bool) -> str:
    """One word: off (a share of zero), under its line, over it, or over it but running on lent share."""
    if row.share <= 0:
        return "off"
    if row.headroom > 0:
        return "under"
    return "lent" if ok else "over"


def _back_under(row: pie.Standing, win: standings.Window, slack: float) -> float | None:
    """When the line, which grows with the week, next reaches this project's estimated spend; None if it won't before
    the window resets."""
    if row.share <= 0:
        return None
    needed = (row.spent - slack * row.share / 100) / row.share
    at = win.resets_at - WEEK_SECONDS + needed * WEEK_SECONDS
    return at if at < win.resets_at else None


def _breakers(ledger, name) -> list[dict]:
    return [{"kind": b["kind"], "since": b["at"], "words": (b["detail"] or b["kind"]).strip()[:240]}
            for b in ledger.open_breakers(name)]


def _is_local(p) -> bool:
    try:
        return bool(p) and handshake._local(p)
    except Exception:
        return False


def _project(ledger, name, now: float, row, ok: bool, win, slack: float, local: bool = False) -> dict:
    """One project's folded section: working now or resting until a time, what landed lately, open breakers."""
    rows = ledger.recent_runs(name, limit=400)
    running = [r for r in rows if r["status"] == "running" and not orphaned(r, now)]
    landed = metrics.landings(rows, now - 7 * 86400, now + 60, local)   # a local board has no merges: a closed item is a landing (commonroom#651)
    breakers = _breakers(ledger, name)
    if running:
        r = running[0]
        status = {"kind": "working", "text": f"working now: {r['role']} {r['task'] or ''}".strip()}
    elif breakers:
        status = {"kind": "stopped", "text": "stopped: " + "; ".join(b["words"] for b in breakers)}
    elif row is not None and row.share <= 0:
        status = {"kind": "off", "text": "off: its share is zero"}
    elif row is not None and row.headroom <= 0 and not ok:
        back = _back_under(row, win, slack) if win else None
        status = {"kind": "resting", "until": back or (win.resets_at if win else None),
                  "text": "resting: over its line, back under it about" if back
                  else "resting: over its line, until the week resets"}
    else:
        status = {"kind": "idle", "text": "nothing running now"}
    return {"status": status, "landed_7d": len(landed),
            "landed_recent": [r["task"] for r in sorted(landed, key=lambda r: -r["started_at"])[:RECENT_LANDINGS]],
            "breakers": breakers}


def _unread(ledger, reg, key: str, now: float) -> dict | None:
    """The project's unread inbox notes (commonroom#588): the count and the oldest; None if they can't be read."""
    try:
        return handshake.unread_summary(reg, reg.projects[key], ledger, now)
    except Exception:
        return None


def _trail(reg, key: str, now: float, board_trail) -> dict:
    """One project's trail (commonroom#408): the owner's notes of the last `TRAIL_DAYS`, newest first, each with the issues
    filed from it. A board that can't be read leaves the notes with no items and says why in `trail_error`."""
    p = reg.projects[key]
    try:
        notes = sorted(handshake.owner_notes(reg, p, now - handshake.TRAIL_DAYS * 86400, now), key=lambda n: -n["at"])
    except Exception as exc:
        return {"trail": [], "trail_error": f"the notes can't be read: {str(exc)[:120]}"}
    error, linked = None, {}
    if not notes:
        pass
    elif not handshake._readable(p):
        error = "no [project] repo in its room.toml"
    else:
        try:
            linked = board_trail(p, notes)
            if isinstance(linked.get("error"), str):   # the dashboard's cache hands back a failure as {"error": ...}
                error, linked = linked["error"][:120], {}
        except Exception as exc:
            error = f"the board can't be read: {str(exc)[:120]}"
    out = [{"name": n["name"], "at": n["at"], "first_line": handshake._first_line(n["text"]),
            "items": [{k: i[k] for k in ("number", "title", "state", "merged")} for i in linked.get(n["name"]) or []]}
           for n in notes]
    return {"trail": out, **({"trail_error": error} if error else {})}


def _local_tasks(p) -> list[dict]:
    """A local-board project's open `owner:human` items, shaped as the queue shapes them; the url is the item's folder."""
    folder = handshake.folder_of(p)
    return [{"number": i["number"], "title": i["title"], "due": board.due_date(i["body"]),
             "url": str(LocalBoard(folder).items / str(i["number"]))}
            for i in LocalBoard(folder).open_issues(None) if any(l["name"] == board.OWNER_LABEL for l in i["labels"])]


def page_state(ledger, cfg: dict, now: float, reg=None, queue=None, transcripts: Path | None = None, trail=None) -> dict:
    """The page as data. `reg`: the registry (default: the room home's); `queue(repo)`: the owner's open tasks there;
    `trail(project, notes)`: the board's issues for those notes (`handshake.trail`; without it sections carry no trail)."""
    problem = None
    try:
        reg = reg if reg is not None else registry_mod.load(registry_mod.default_path())
    except (registry_mod.RegistryError, OSError) as exc:
        reg, problem = registry_mod.Registry(), f"the project registry can't be read: {exc}"
    host = cfg["project"].get("name")
    slack = config_mod.DEFAULTS["budget"].get(PROVIDER, {}).get("weekly_slack", 0)
    report, names, error = None, {}, problem
    if reg.projects:
        try:
            names = standings.ledger_names(reg)
            report = standings.standings(ledger, reg, PROVIDER, now, transcripts)
        except Exception as exc:   # the page says so rather than going blank
            error = f"the pie can't be computed: {exc}"
    win = report.window if report else standings.window(ledger, PROVIDER, now)
    week = None
    if win:
        age = now - win.observed_at
        week = {"week_gone_pct": win.elapsed * 100, "plan_used_pct": win.used_pct, "resets_at": win.resets_at,
                "reading_at": win.observed_at, "reading_age_seconds": age,
                "stale": age > cfg.get("freshness_seconds", config_mod.DEFAULTS["freshness_seconds"])}
    gate_v = verdicts(ledger, cfg, now)[PROVIDER]
    holds = pie_holds(ledger, cfg, {PROVIDER: gate_v}, now, transcripts) if reg.projects else None
    gate = {"go": gate_v.allowed, "reason": gate_v.reason}
    if holds is None:
        pie_verdict = None
    elif not gate_v.allowed:
        pie_verdict = {"go": None, "reason": "not asked: the gate holds first"}
    else:
        pie_verdict = {"go": PROVIDER not in holds, "reason": holds.get(PROVIDER, "no hold on new model work")}
    rows, sections = [], []
    if report:
        active = pie_active(ledger, names, PROVIDER, now)
        for s in report.rows:
            ok = pie.may_start(report.rows, active, s.name)[0]
            rows.append({"name": s.name, "share": s.share, "spent_est": s.spent, "room_est": report.room(s.name),
                         "own_sessions_est": report.own.get(s.name, 0.0), "line": s.allowed, "state": _state_word(s, ok),
                         "when": s.when,
                         "work": reg.projects[s.name].work if s.name in reg.projects else None})
            sections.append({"name": s.name, **_project(ledger, names[s.name], now, s, ok, win, slack, _is_local(reg.projects.get(s.name))),
                             "unread": _unread(ledger, reg, s.name, now),
                             **(_trail(reg, s.name, now, trail) if trail and s.name in reg.projects else {})})
        rows.append({"name": "you (unassigned)", "share": report.owner_share, "spent_est": report.owner_use,
                     "room_est": None, "own_sessions_est": None, "line": report.owner_share * win.elapsed, "state": "yours",
                     "when": None, "work": None})
    elif not reg.projects:
        sections.append({"name": host or "this project", **_project(ledger, host, now, None, True, win, slack, config_mod.board_kind(cfg) == "local")})
    waiting = []
    local = set()
    for key, p in reg.projects.items():
        try:
            if handshake._local(p):
                local.add(key)
                waiting += [{"project": key, **t} for t in _local_tasks(p)]
        except Exception:
            continue
    if queue:
        repos = {}
        for key, p in reg.projects.items():
            if key in local:
                continue
            try:
                repos[key] = config_mod.load(Path(p.path) / "room.toml", follow_host=False)["project"].get("repo")
            except Exception:
                continue
        if not reg.projects:
            repos[host or "this project"] = cfg["project"].get("repo")
        seen = set()
        for key, repo in repos.items():
            if not repo or repo in seen:
                continue
            seen.add(repo)
            found = queue(repo)
            for t in found if isinstance(found, list) else []:
                waiting.append({"project": key, "number": t["number"], "title": t["title"], "due": t.get("due"),
                                "url": t.get("url")})
    try:
        routes = ledger.routes(30)
    except Exception:
        routes = []
    return {"now": now, "routes": routes, "limits": limits.in_force(ledger.path.parent),
            "week": week, "gate": gate, "pie": pie_verdict, "rows": rows, "waiting": waiting,
            "projects": sections, "registered": bool(reg.projects), "error": error}
