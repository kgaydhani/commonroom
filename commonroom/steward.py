"""The ledger invariants as pure functions (commonroom#322, axiom A3: code holds obligations, models hold none).

Each takes a board snapshot (the issues and PRs as `gh` lists them, with `state`, `labels`, `body`) and returns
findings: dicts with `invariant` (a slug), `number` and `detail` (one plain sentence a person can act on). Nothing here
calls `gh`, reads the clock or changes anything; `now` is an argument. Wiring into the beat is a separate issue."""
from __future__ import annotations

import datetime
import re

from commonroom import dispatch

PLACED = {"status:ready", "status:in-progress", "status:review", "status:blocked", "parked", "needs:director",
          "owner:human", dispatch.EXTERNAL}
DUE = re.compile(r"^\s*Due:\s*(\d{4}-\d{2}-\d{2})\b", re.I | re.M)


def finding(invariant: str, number: int, detail: str) -> dict:
    return {"invariant": invariant, "number": number, "detail": detail}


def _open(issue: dict) -> bool:
    return issue.get("state", "OPEN") == "OPEN"


def unplaced(issues: list[dict]) -> list[dict]:
    """Open issues with none of the placing labels: the dispatcher can't see them."""
    return [finding("unplaced", i["number"], f"#{i['number']} is open in the milestone with no status or parking label, "
                    "so the dispatcher can't see it.")
            for i in issues if _open(i) and not dispatch.labels(i) & PLACED]


def blocked_on_nothing(issues: list[dict]) -> list[dict]:
    """`status:blocked` with no director or owner hold, and no dependency still open (or none named)."""
    state = {i["number"]: i.get("state", "OPEN") for i in issues}
    found = []
    for i in issues:
        names = dispatch.labels(i)
        if not _open(i) or "status:blocked" not in names or names & {"needs:director", "owner:human"}:
            continue
        deps = dispatch.dependencies(i.get("body"))
        if all(state.get(d) == "CLOSED" for d in deps):
            why = "every issue it depends on is closed" if deps else "it names no issue it waits for"
            found.append(finding("blocked-on-nothing", i["number"], f"#{i['number']} is blocked but {why}."))
    return found


def orphan_prs(issues: list[dict], prs: list[dict]) -> list[dict]:
    """Open PRs on an `i<N>-*` branch whose issue #N is closed."""
    closed = {i["number"] for i in issues if not _open(i)}
    found = []
    for pr in prs:
        m = dispatch.BRANCH.match(pr.get("headRefName") or "")
        if m and int(m.group(1)) in closed and pr.get("state", "OPEN") == "OPEN":
            found.append(finding("orphan-pr", pr["number"], f"PR #{pr['number']} is open but its issue "
                                 f"#{m.group(1)} is closed."))
    return found


def stale_claim_findings(issues: list[dict], prs: list[dict], running_runs: list[dict], now: float) -> list[dict]:
    return [finding("stale-claim", c["number"], f"#{c['number']} has been in progress for {c['minutes']} minutes "
                    "with no run holding it.") for c in dispatch.stale_claims(issues, prs, running_runs, now)]


def owner_overdue(issues: list[dict], now: float) -> list[dict]:
    """Open `owner:human` issues whose `Due: YYYY-MM-DD` line is before today (UTC)."""
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
    found = []
    for i in issues:
        if not _open(i) or "owner:human" not in dispatch.labels(i) or not (m := DUE.search(i.get("body") or "")):
            continue
        try:
            due = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if due < today:
            found.append(finding("owner-overdue", i["number"], f"#{i['number']} was due on {due} and is still open."))
    return found


def check(issues: list[dict], prs: list[dict], running_runs: list[dict], now: float) -> list[dict]:
    """Every invariant over one snapshot; an empty list means the board is clean."""
    return (unplaced(issues) + blocked_on_nothing(issues) + orphan_prs(issues, prs)
            + stale_claim_findings(issues, prs, running_runs, now) + owner_overdue(issues, now))
