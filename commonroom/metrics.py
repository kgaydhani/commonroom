"""What each landed change costs, from ledger run rows alone (commonroom#211): input tokens split into uncached,
cache-write and cache-read, review runs and CHANGES rounds (also by the model that built the PR), runs by role, and
minutes per landing, plus coordination's share of input tokens and run time, over 7 and 30 days.
Pure functions over rows: no GitHub calls, no clock reads."""
from __future__ import annotations

import re

DAY = 86400
WINDOWS = {"7d": 7 * DAY, "30d": 30 * DAY}
LOOKBACK = 37 * DAY          # rows to load: the longest window plus a week, so its PRs' builders are found
ROOM_ROLES = ("merge", "update", "carry")   # rows the room writes itself: no model ran (carry: commonroom#226)
COORDINATION_ROLES = ("director", "auditor")
NOT_WORK = ("running", "room-error")     # unfinished, or failed before any model work
PR_REF = re.compile(r"PR #(\d+)")


def landings(rows: list[dict], since: float, until: float) -> list[dict]:
    """The landed changes in [since, until]: today the ledger's `merge` rows. Every per-landing figure divides by
    these, so a project that declares its own landed events changes this function and nothing else."""
    return [r for r in rows if r["role"] == "merge" and since <= r["started_at"] <= until]


def model_runs(rows: list[dict], since: float, until: float) -> list[dict]:
    """Finished runs in which a model worked, started in [since, until]. A resumed run is a run of its own."""
    return [r for r in rows if r["role"] not in ROOM_ROLES and r["status"] not in NOT_WORK
            and since <= r["started_at"] <= until]


def pr_of(row: dict) -> int | None:
    for text in [*(o.get("ref") or "" for o in row.get("outputs") or []), row.get("task") or ""]:
        match = PR_REF.search(text)
        if match:
            return int(match.group(1))
    return None


def verdict(row: dict) -> str | None:
    """APPROVE, CHANGES or INCOMPLETE from a review run's output (`PR #N CHANGES`), else None."""
    for o in row.get("outputs") or []:
        if o.get("kind") == "review":
            return str(o.get("ref") or "").rsplit(" ", 1)[-1] or None
    return None


def builder_models(rows: list[dict]) -> dict[int, str]:
    """PR number -> `provider:model` of the latest run that opened or updated it (its `pr` output), else of the latest
    run on the task the PR merged for."""
    by_pr, by_task, merged_task = {}, {}, {}
    for r in sorted(rows, key=lambda r: r["started_at"]):
        label = f"{r['provider']}:{r['model']}"
        if r["role"] == "merge":
            n = pr_of(r)
            if n is not None:
                merged_task[n] = r.get("task")
        elif r["role"] not in ROOM_ROLES and r["role"] != "reviewer":
            for o in r.get("outputs") or []:
                match = PR_REF.search(o.get("ref") or "") if o.get("kind") == "pr" else None
                if match:
                    by_pr[int(match.group(1))] = label
            if r["role"] in ("builder", "scout") and r.get("task"):
                by_task[r["task"]] = label
    models = {n: by_task[t] for n, t in merged_task.items() if t in by_task}
    models.update(by_pr)
    return models


def per(total: float, count: int, digits: int = 2):
    return round(total / count, digits) if count else None


def window(rows: list[dict], since: float, until: float, models: dict[int, str] | None = None) -> dict:
    landed, work = landings(rows, since, until), model_runs(rows, since, until)
    n = len(landed)
    models = builder_models(rows) if models is None else models
    coordination = [r for r in work if r["role"] in COORDINATION_ROLES]
    tokens = sum(r.get("input_tokens") or 0 for r in work)
    minutes = sum(((r.get("ended_at") or r["started_at"]) - r["started_at"]) / 60 for r in work)
    coordination_tokens = sum(r.get("input_tokens") or 0 for r in coordination)
    coordination_minutes = sum(((r.get("ended_at") or r["started_at"]) - r["started_at"]) / 60
                               for r in coordination)
    split = [r for r in work if r.get("cache_read_tokens") is not None or r.get("cache_write_tokens") is not None]
    read = sum(r.get("cache_read_tokens") or 0 for r in split)
    write = sum(r.get("cache_write_tokens") or 0 for r in split)
    uncached = sum(max(0, (r.get("input_tokens") or 0) - (r.get("cache_read_tokens") or 0)
                       - (r.get("cache_write_tokens") or 0)) for r in split)
    reviews = [r for r in work if r["role"] == "reviewer"]
    by_role: dict[str, int] = {}
    for r in work:
        by_role[r["role"]] = by_role.get(r["role"], 0) + 1
    by_model: dict[str, dict] = {}
    def bucket(pr):
        return by_model.setdefault(models.get(pr, "unknown") if pr is not None else "unknown",
                                   {"landings": 0, "review_runs": 0, "changes_rounds": 0})
    for r in landed:
        bucket(pr_of(r))["landings"] += 1
    for r in reviews:
        b = bucket(pr_of(r))
        b["review_runs"] += 1
        b["changes_rounds"] += verdict(r) == "CHANGES"
    for b in by_model.values():
        b["review_runs_per_merge"] = per(b["review_runs"], b["landings"])
        b["changes_rounds_per_merge"] = per(b["changes_rounds"], b["landings"])
    return {
        "merges": n, "runs": len(work),
        "input_tokens_per_merge": per(tokens, n, 0),
        # Keep fractions unrounded so a target comparison uses the measured share.
        "coordination_share": coordination_tokens / tokens if tokens else None,
        "coordination_share_minutes": coordination_minutes / minutes if minutes else None,
        # Only runs that recorded the split count here; None (n/a) when no run in the window did.
        "uncached_per_merge": per(uncached, n, 0) if split else None,
        "cache_write_per_merge": per(write, n, 0) if split else None,
        "cache_read_per_merge": per(read, n, 0) if split else None,
        "runs_with_cache_split": len(split),
        "review_runs_per_merge": per(len(reviews), n),
        "changes_rounds_per_merge": per(sum(verdict(r) == "CHANGES" for r in reviews), n),
        "runs_per_merge": per(len(work), n),
        "runs_per_merge_by_role": {role: per(count, n) for role, count in sorted(by_role.items())},
        "minutes_per_merge": per(minutes, n, 1),
        "by_builder_model": dict(sorted(by_model.items())),
    }


def readable(value: float) -> str:
    """A number for a person: 26.5M, 135k, 2.5 or 2 (the dashboard's style)."""
    if abs(value) >= 1e6:
        return f"{value / 1e6:.1f}M"
    if abs(value) >= 1e4:
        return f"{value / 1e3:.0f}k"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def over_target(figures: dict, targets: dict) -> list[str]:
    """`<metric> <value> > <target>` for each target the window's figures exceed; n/a never counts as over."""
    flags = []
    for key, target in (targets or {}).items():
        value = figures.get(key)
        if target is not None and value is not None and value > target:
            flags.append(f"{key} {readable(value)} > {readable(target)}")
    return flags


def report(rows: list[dict], now: float, targets: dict) -> dict:
    """One project's per-merge figures over each window, its targets, and which windows are over them."""
    models = builder_models(rows)
    result = {"targets": dict(targets or {})}
    flags = []
    for name, seconds in WINDOWS.items():
        result[name] = window(rows, now - seconds, now, models)
        flags += [f"{name}: {flag}" for flag in over_target(result[name], targets)]
    result["over_target"] = flags
    return result


def project_report(ledger, project: str | None, cfg: dict, now: float) -> dict:
    return report(ledger.runs_since(project, now - LOOKBACK), now, cfg.get("targets") or {})
