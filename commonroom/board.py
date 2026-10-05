"""The board is GitHub: issues, labels and milestones, read through the `gh` CLI."""
from __future__ import annotations

import json
import shutil
import subprocess
import time

OWNER_LABEL = "owner:human"
READY_LABEL = "status:ready"


def gh(args: list[str], timeout: float = 30) -> object:
    if not shutil.which("gh"):
        raise RuntimeError("GitHub CLI (gh) not found")
    # GitHub has transient failures (GraphQL "Something went wrong", 5xx, rate limits): retry with backoff.
    for attempt in range(4):
        try:
            done = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        except subprocess.TimeoutExpired:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt * 2)
            continue
        transient = any(s in (done.stderr or "") for s in ("Something went wrong", "502", "503", "504", "timeout",
                                                             "rate limit", "connection reset", "EOF"))
        if done.returncode == 0 or not transient or attempt == 3:
            break
        time.sleep(2 ** attempt * 2)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"gh {' '.join(args)} failed")
    out = done.stdout.strip()
    if not out:
        return None
    return json.loads(out) if out[0] in "[{" else out


def issues(repo: str, label: str | None = None, milestone: str | None = None, state: str = "open", limit: int = 200) -> list[dict]:
    args = ["issue", "list", "--repo", repo, "--state", state, "--limit", str(limit),
            "--json", "number,title,labels,assignees,milestone,updatedAt,state,body,comments"]
    if label:
        args += ["--label", label]
    if milestone:
        args += ["--milestone", milestone]
    return gh(args) or []


def milestone(repo: str, title: str) -> dict | None:
    for m in gh(["api", f"repos/{repo}/milestones?state=all&per_page=100"]) or []:
        if m["title"] == title:
            return m
    return None


def due_date(body: str | None) -> str | None:
    """Owner tasks carry a `Due: YYYY-MM-DD` line; GitHub issues have no native due date."""
    for line in (body or "").splitlines():
        if line.strip().lower().startswith("due:"):
            return line.split(":", 1)[1].strip()
    return None
