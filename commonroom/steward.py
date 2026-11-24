"""The ledger invariants as pure functions (commonroom#322, axiom A3: code holds obligations, models hold none).

Each takes a board snapshot (the issues and PRs as `gh` lists them, with `state`, `labels`, `body`) and returns
findings: dicts with `invariant` (a slug), `number` and `detail` (one plain sentence a person can act on). Nothing here
calls `gh`, reads the clock or changes anything; `now` is an argument. Wiring into the beat is a separate issue."""
from __future__ import annotations

import datetime
import re

from commonroom import dispatch

PLACED = {"status:ready", "status:in-progress", "status:review", "status:blocked", "parked", "needs:director",
          "owner:human", "claim:external"}   # the last is dispatch.EXTERNAL (dispatch imports this module: no cycle at load)
DUE = re.compile(r"^\s*Due:\s*(\d{4}-\d{2}-\d{2})\b", re.I | re.M)


def finding(invariant: str, number: int, detail: str) -> dict:
    return {"invariant": invariant, "number": number, "detail": detail}


def _open(issue: dict) -> bool:
    return issue.get("state", "OPEN") == "OPEN"


def unplaced(issues: list[dict], milestone: str | None = None) -> list[dict]:
    """Open issues in the goal milestone (the only ones the dispatcher reads) with none of the placing labels.
    With no milestone configured it reports nothing: issues outside it are a backlog, not a fault."""
    if not milestone:
        return []
    return [finding("unplaced", i["number"], f"#{i['number']} is open in the milestone with no status or parking label, "
                    "so the dispatcher can't see it.")
            for i in issues if _open(i) and (i.get("milestone") or {}).get("title") == milestone
            and not dispatch.labels(i) & PLACED
            and "kind:follow-up" not in dispatch.labels(i)]   # a reviewer follow-up waits for the director's release (#485)


def _epoch(stamp: str | None) -> float | None:
    try:
        return datetime.datetime.fromisoformat((stamp or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def unreconciled(issues: list[dict], direction_at: float, reconciled: dict[int, float],
                 milestone: str | None = None) -> list[dict]:
    """Open goal-milestone issues created before the latest direction with no `reconciled:<n>` mark at or after it
    (`reconciled` maps issue number to the mark's time): the owner changed course and nobody ruled on them (commonroom#355)."""
    if not milestone:
        return []
    return [finding("unreconciled", i["number"], f"#{i['number']} was opened before the latest change of direction "
                    "and has no verdict since: keep, park or cancel it with `room reconcile`.")
            for i in issues if _open(i) and (i.get("milestone") or {}).get("title") == milestone
            and (c := _epoch(i.get("createdAt"))) is not None and c < direction_at
            and reconciled.get(i["number"], float("-inf")) < direction_at]


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


def trial_final(reports: list, ) -> list[dict]:
    """A finding for each active trial whose verdict is final and which has no `ended` date: the director adopts or
    reverts it (commonroom#353). `reports` is `trials.report(...)`; a running trial gives none."""
    return [finding("trial-final", n, f"Trial {t.name} has a final verdict, {res['verdict']}, and no ended date: "
                    "adopt or revert it, then set `ended` in trials.toml.")
            for n, (t, res) in enumerate(reports, 1) if res["verdict"] != "running" and t.ended is None]


def heartbeat_failing(runs: dict[str, list[int]], needed: int, configured: list[str]) -> list[dict]:
    """A finding for each heartbeat command whose last `needed` runs all exited non-zero; one success clears it
    (commonroom#587). `runs` maps a command to its exit codes, newest first; `configured` is the project's current commands, joined as
    `services.tick` records them: a command no longer configured has no finding. The number is its place in `configured`."""
    return [finding("heartbeat-failing", n, f"Heartbeat command `{cmd}` exited non-zero {needed} times running; "
                    f"last exit code {codes[0]}.")
            for n, cmd in enumerate(configured, 1) if (codes := runs.get(cmd, []))
            if len(codes) >= needed and all(c != 0 for c in codes[:needed])]


def check(issues: list[dict], prs: list[dict], running_runs: list[dict], now: float,
          milestone: str | None = None, direction_at: float | None = None,
          reconciled: dict[int, float] | None = None) -> list[dict]:
    """Every invariant over one snapshot; an empty list means the board is clean. `direction_at` is the project's
    latest direction mark, None when it has none (then nothing about directions is checked)."""
    found = (unplaced(issues, milestone) + blocked_on_nothing(issues) + orphan_prs(issues, prs)
             + stale_claim_findings(issues, prs, running_runs, now) + owner_overdue(issues, now))
    if direction_at is not None:
        found += unreconciled(issues, direction_at, reconciled or {}, milestone)
    return found
