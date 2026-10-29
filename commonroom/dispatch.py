"""Turn the board into work. Deterministic: decides what runs; models only do the work.

One pass (`run_once`):
1. Housekeeping with no model calls: merge approved green PRs, unblock issues whose dependencies closed.
2. Pick one piece of model work, highest value first: review a waiting PR, build a ready issue, or run the
   director on a trigger. Each needs an allowed provider for its role.
3. Run it, record usage and outputs, update the board, check breakers.
"""
from __future__ import annotations

import calendar
import datetime
import fnmatch
import os
import json
import re
import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

# Every room module is imported here, at start, never inside a function: the room fast-forwards its own live checkout
# after merging its own PRs, and a long-running dispatcher must never load a newer module next to its older ones.
from commonroom import board as board_mod, config as config_mod, gate, notify as notify_mod, services, usage, workers as workers_mod
from commonroom.ledger import Ledger
from commonroom.lock import acquire_lock
from commonroom.workers import load_role, run_worker, worker_tmp

DEPENDS = re.compile(r"^(?:Depends on|Blocked by)\b(.*)$", re.I | re.M)
# The list after "Depends on": references joined by commas or "and", each optionally followed by a parenthetical.
DEPENDS_ITEM = re.compile(r"\s*(?:#(\d+)|\([^)]*\)|issues?\b|and\b|[,&:])", re.I)
BRANCH = re.compile(r"^i(\d+)-")
MARK = "[room]"
STATUS_LINE = re.compile(r"STATUS:\s*(DONE|BLOCKED)", re.I)
ACTIONS_LINE = re.compile(r"ACTIONS:\s*(\[.*\])", re.S)
INTERACTIVE_PHRASE = "interactive session"
DIRECTOR_COOLDOWN = 3600
STEER_REASON = "periodic steering: check pace to the goal, the bottleneck and the allocation; write the Director log"
BULLETIN_REASON = ("bulletin missed (due {due}): post a short text digest to #{issue} starting with @{owner} (merges since "
                   "the last bulletin, open problems, a pointer to the Director log), and add a Director log entry saying "
                   "the interactive session's schedules need recreating")
OWNER_COOLDOWN = 300
ROOM_ERROR_BACKOFF = 600   # a task whose last run was a room-error rests this long, doubling with each one in a row...
ROOM_ERROR_ESCALATE = 3    # ...and the third in a row goes to the director (commonroom#9)
SCOUT_INTERLEAVE = 3 * 3600  # a ready research task jumps the build queue at most this often
# A scheduled issue's body carries this, so the schedule sees its last one is still open (commonroom#151).
SCHEDULE_MARK = "<!-- room-schedule:{role} -->"
NOTIFY_MAX = 5   # at most this many mention notifications per pass; the rest are summed up in one
# Roles the room dispatches through their own triggers. Any other role with a `[roles]` entry and an agent file runs on
# demand from a `role:<name>` + `status:ready` issue (commonroom#4).
BUILT_IN_ROLES = {"builder", "scout", "director", "reviewer", "auditor"}
PLAYTEST_TITLE = "Playtest after merges:"   # the room's playtest issues (commonroom#53)
PLAYTEST_PR = re.compile(r"^- PR #(\d+)\b", re.M)
STALE_CLAIM_MINUTES = 30   # an in-progress issue no run holds, unchanged this long, goes back to ready (commonroom#63)
# Someone outside the room (the interactive director's local agents, a cloud session) owns the issue: the room never
# builds it or puts status:ready on it, but still reviews and merges its PR (commonroom#132).
EXTERNAL = "claim:external"
# Issues that re-derive shared golden data conflict when built in parallel: only one is in progress or review at a time (commonroom#52).
GOLDEN = "risk:golden"
# Checking a run's ACTIONS claims against GitHub (commonroom#68): the subjects after the first `#N`, the evidence each
# kind of claim needs (the word nearest before the subjects wins, else the first after them: commonroom#99; "skip" for
# edits, which leave no timeline event), and how many subjects one run may read.
CLAIM_MORE = re.compile(r"(?:[\s,/]|\band\b)*#(\d+)\b", re.I)
CLAIM_EVIDENCE = ((("creat", "opened", "filed"), "created"), (("close",), {"closed"}), (("merge",), {"merged"}),
                  (("comment",), {"commented"}), (("amend", "edit", "rewr", "reword", "rescop", "re-scop"), "skip"),
                  (("→", "->", "label", "pinned", "status:", "prio:", "ready", "blocked", "park"),
                   {"labeled", "unlabeled"}))
CLAIM_SUBJECTS_MAX = 20


def priority(issue: dict) -> tuple:
    """prio:top (foundation work others build on, e.g. a milestone plan) first, then prio:high, prio:low last,
    otherwise oldest first. An issue labelled needs:changes (a PR the reviewer sent back) ranks one tier above its
    own and before the other issues in that tier (among bounced issues: their lifted tier, then own tier, then
    oldest), so an almost-done PR finishes before new work starts (commonroom#121) but a prio:low fix round doesn't
    jump ahead of prio:high work (commonroom#170): prio:low ranks as the default tier, the default as prio:high,
    and prio:high and prio:top stay where they are. Housekeeping keeps the label in step with the PR's last review,
    so this stays a function of labels alone."""
    names = labels(issue)
    tier = -1 if "prio:top" in names else 0 if "prio:high" in names else 2 if "prio:low" in names else 1
    if "needs:changes" not in names:
        return (tier, 1, tier, issue["number"])
    return (tier if tier <= 0 else tier - 1, 0, tier, issue["number"])


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")


def labels(issue: dict) -> set[str]:
    return {l["name"] for l in issue.get("labels", [])}


# An issue in review carries no other status and no needs:changes: gamemirage#427 had ready, review and needs:changes
# at once, and a ready label invites a second builder (commonroom#93).
LEAVES_REVIEW = ("status:ready", "status:in-progress", "status:blocked", "needs:changes")


def dependencies(body: str | None) -> set[int]:
    """Only the leading list of references counts. Briefs follow it with prose ("Depends on #89. Performance is
    checked separately in #132") or asides ("#279 (PR #369)"), and a reference there is not a dependency: it can
    never close, or it depends back on this issue, so the auto-unblock never fires (commonroom#12)."""
    deps = set()
    for rest in DEPENDS.findall(body or ""):
        pos = 0
        while m := DEPENDS_ITEM.match(rest, pos):
            if m.group(1):
                deps.add(int(m.group(1)))
            pos = m.end()
    return deps


def stale_claims(issues: list[dict], prs: list[dict], running_runs: list[dict], now: float) -> list[dict]:
    """Open `status:in-progress` issues that no run holds and that haven't changed for STALE_CLAIM_MINUTES: a moonshot
    the pace gate stopped a minute after claiming gamemirage#296 left it in progress for 1.5 h (commonroom#63). A running
    run on `#N` holds it, any running moonshot holds them all (it may be on any issue), and an open `i<N>-*` PR means
    it's in review whatever its label says."""
    if any(r["role"] == "moonshot" for r in running_runs):
        return []
    held = {r["task"] for r in running_runs if r.get("task")}
    in_review = {int(m.group(1)) for pr in prs if (m := BRANCH.match(pr.get("headRefName") or ""))}
    stale = []
    for issue in issues:
        n, updated = issue["number"], issue.get("updatedAt")
        if (issue.get("state", "OPEN") != "OPEN" or "status:in-progress" not in labels(issue) or not updated
                or EXTERNAL in labels(issue) or n in in_review or f"#{n}" in held):
            continue
        minutes = int((now - calendar.timegm(time.strptime(updated[:19], "%Y-%m-%dT%H:%M:%S"))) // 60)
        if minutes >= STALE_CLAIM_MINUTES:
            stale.append({"number": n, "title": issue.get("title"), "minutes": minutes})
    return stale


def stale_ci_runs(runs: list[dict], prs: list[dict]) -> list[tuple[int, str]]:
    """Queued `pull_request` runs nobody will read (commonroom#138): (run id, reason) for one whose branch has no open PR
    (merged or closed), or whose sha isn't its open PR's current head (a newer push replaced it). Other events and
    runs already past `queued` are never cancelled."""
    heads = {pr["headRefName"]: pr.get("headRefOid") for pr in prs}
    stale = []
    for run in runs:
        if run.get("event") != "pull_request" or run.get("status", "queued") != "queued":
            continue
        branch = run.get("headBranch") or ""
        if branch not in heads:
            stale.append((run["databaseId"], f"run {run['databaseId']} on {branch}: its PR is merged or closed"))
        elif heads[branch] and run.get("headSha") and run["headSha"] != heads[branch]:
            stale.append((run["databaseId"], f"run {run['databaseId']} on {branch}: a newer push replaced {run['headSha'][:7]}"))
    return stale


def claim_subjects(claim: str, other_projects=()) -> list[int] | None:
    """The issues or PRs a claim is about: its first `#N` and any directly after it. None when it has no subject in this
    repository: no `#N`, or one written with a repo prefix (`name#N`) or after another project's name."""
    found = claim_span(claim, other_projects)
    return found and found[0]


def claim_span(claim: str, other_projects=()) -> tuple[list[int], int, int] | None:
    """`claim_subjects`, with where the subjects start and end in the claim."""
    first = re.search(r"#(\d+)", claim)
    if not first or (first.start() and re.match(r"[\w./-]", claim[first.start() - 1])):
        return None
    before = claim[:first.start()].lower()
    if any(re.search(rf"(?<![\w-]){re.escape(p.lower())}\s+(?:(?:pr|issue)\s+)?$", before) for p in other_projects if p):
        return None
    subjects, at = [int(first.group(1))], first.end()
    while m := CLAIM_MORE.match(claim, at):
        subjects.append(int(m.group(1)))
        at = m.end()
    return subjects, first.start(), at


def claim_rule(claim: str, start: int, end: int):
    """The evidence a claim needs, from the verb that governs its subjects (commonroom#99): the keyword nearest before
    the first subject, else the first one after the subjects. None when it has no recognised verb."""
    low = claim.lower()
    found = [(low.rfind(w, 0, start), rule) for words, rule in CLAIM_EVIDENCE for w in words]
    at, rule = max(found, key=lambda f: f[0])
    if at >= 0:
        return rule
    found = [(low.find(w, end), rule) for words, rule in CLAIM_EVIDENCE for w in words]
    return min(((at, rule) for at, rule in found if at >= 0), default=(0, None), key=lambda f: f[0])[1]


def check_claims(claims: list, since: float, fetch, other_projects=()) -> tuple[list[str], list[str]]:
    """Check ACTIONS claims against GitHub evidence dated after `since`. `fetch(number, kind)` returns the issue or PR
    (`kind="issue"`, for `created_at`) or its timeline events (`kind="timeline"`). Returns (checked, unverified) claims.
    A skipped claim (another repository, past the subject limit, no recognised verb, or an edit) or one whose read
    failed is in neither."""
    cache: dict[tuple[int, str], object] = {}

    def read(n: int, kind: str):
        if (n, kind) not in cache:
            try:
                cache[(n, kind)] = fetch(n, kind)
            except Exception:
                cache[(n, kind)] = None
        return cache[(n, kind)]

    def after(stamp) -> bool:
        try:
            return calendar.timegm(time.strptime(str(stamp or "")[:19], "%Y-%m-%dT%H:%M:%S")) >= since
        except ValueError:
            return False

    seen: set[int] = set()
    checked, unverified = [], []
    for claim in map(str, claims):
        found = claim_span(claim, other_projects)
        if not found:
            continue
        subjects, start, end = found
        wanted = claim_rule(claim, start, end)
        if wanted in (None, "skip") or len(seen | set(subjects)) > CLAIM_SUBJECTS_MAX:
            continue
        seen |= set(subjects)
        results = []
        for n in subjects:
            if wanted == "created":
                record = read(n, "issue")
                results.append(None if record is None else after(record.get("created_at")))
                continue
            events = read(n, "timeline")
            results.append(None if events is None else any(
                e.get("event") in wanted and after(e.get("created_at") or e.get("submitted_at"))
                for e in events))
        if False in results:
            checked.append(claim)
            unverified.append(claim)
        elif None not in results:
            checked.append(claim)
    return checked, unverified


def actions_of(text: str | None) -> list:
    m = ACTIONS_LINE.search(text or "")
    try:
        return json.loads(m.group(1)) if m else []
    except ValueError:
        return []


def runnable_roles(cfg: dict) -> set[str]:
    """Roles with a `[roles]` entry (a provider list) and an agent file `.claude/agents/<name>.md` in the project."""
    root = cfg.get("_root")
    return {name for name, spec in (cfg.get("roles") or {}).items()
            if isinstance(spec, list) and root and (Path(root) / ".claude" / "agents" / f"{name}.md").is_file()}


def in_window(window: str | None, now: float) -> bool:
    """Is local time `now` inside "HH:MM-HH:MM" (start inclusive, end exclusive; may wrap midnight)? Unset or
    malformed means any time."""
    try:
        start, end = ((int(h) * 60 + int(m)) for h, m in (part.split(":") for part in str(window).split("-")))
    except ValueError:
        return True
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    return start <= minute < end if start <= end else (minute >= start or minute < end)


def find_bash() -> str | None:
    """Git for Windows' bash. Checked before PATH, which can hold WSL's bash.exe and which is minimal under Task Scheduler."""
    import shutil
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists() and "system32" not in candidate.lower():
            return candidate
    return None


NETWORK_GIT = {"push", "fetch", "clone", "ls-remote"}
GIT_REJECTED = ("non-fast-forward", "fetch first", "[rejected]", "Authentication failed", "not found",
                "does not appear to be a git repository")


def git_env(cwd) -> dict:
    """Git never looks above `cwd` for a repository. From a worktree whose `.git` link was deleted, git otherwise finds
    the owner's main checkout one level up and commits there (commonroom#17); with the ceiling it fails instead."""
    return {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(cwd).resolve().parent)}


def git(args: list[str], cwd: Path) -> str:
    # GitHub has transient failures (e.g. "remote: error: unable to write file ... index-pack failed"): retry network ops.
    tries = 3 if args and args[0] in NETWORK_GIT else 1
    for attempt in range(tries):
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=git_env(cwd))
        if done.returncode == 0 or any(s in done.stderr for s in GIT_REJECTED):
            break
        if attempt < tries - 1:
            time.sleep(5 * (attempt + 1) ** 2)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def merges_back_to(commits: list[dict], head: str, stamp: str) -> bool:
    """True when walking first parents from `head` reaches `stamp` and every commit on the way is a merge. Commits that
    main brought in sit behind the merges' second parents and are not on this chain."""
    by_sha = {c["sha"].lower(): [p.lower() for p in c["parents"]] for c in commits}
    cur = head
    for _ in range(len(by_sha) + 1):
        if cur and cur.startswith(stamp):
            return True
        parents = by_sha.get(cur)
        if not parents or len(parents) < 2:
            return False
        cur = parents[0]
    return False


# Who opened a PR and where its head lives, so the room acts only on its trusted logins' own branches (commonroom#176).
PR_ORIGIN = "author,isCrossRepository,headRepositoryOwner"


class GitHubBoard:
    """Board operations through `gh`. Swappable for a fake in tests."""

    def __init__(self, repo: str):
        self.repo = repo

    def open_issues(self, milestone: str | None) -> list[dict]:
        return board_mod.issues(self.repo, milestone=milestone)

    def all_issues(self) -> list[dict]:
        return board_mod.gh(["issue", "list", "--repo", self.repo, "--state", "all", "--limit", "500",
                             "--json", "number,state,labels,title,body,comments,updatedAt,closedAt,milestone,author"],
                            # 2 MB of JSON: 16 s on a quiet PC, over the 30 s default when the runner is loaded.
                            timeout=120) or []

    def set_labels(self, number: int, add=(), remove=()):
        args = ["issue", "edit", str(number), "--repo", self.repo]
        for name in add:
            args += ["--add-label", name]
        for name in remove:
            args += ["--remove-label", name]
        if len(args) > 4:
            board_mod.gh(args)

    def set_milestone(self, number: int, title: str):
        board_mod.gh(["issue", "edit", str(number), "--repo", self.repo, "--milestone", title])

    def ensure_label(self, name: str):
        """`issue edit --add-label` fails on a label the repo doesn't have yet: create it, keeping one that exists."""
        try:
            board_mod.gh(["label", "create", name, "--repo", self.repo])
        except RuntimeError as exc:
            if "already exists" not in str(exc):
                raise

    def comment(self, number: int, body: str):
        board_mod.gh(["issue", "comment", str(number), "--repo", self.repo, "--body", f"{MARK} {body}"])

    def close_issue(self, number: int, body: str):
        board_mod.gh(["issue", "close", str(number), "--repo", self.repo, "--comment", f"{MARK} {body}"])

    def edit_issue(self, number: int, title: str, body: str):
        board_mod.gh(["issue", "edit", str(number), "--repo", self.repo, "--title", title, "--body", body])

    def issue_comments(self, number: int, since: float) -> list[dict]:
        """Every comment on an issue updated since `since`, as `createdAt`, `body` and `author`: `issue list` stops at 100."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
        pages = board_mod.gh(["api", "--paginate", "--slurp", f"repos/{self.repo}/issues/{number}/comments?since={stamp}&per_page=100"])
        return [{"createdAt": c.get("created_at"), "body": c.get("body"), "author": {"login": (c.get("user") or {}).get("login")}}
                for page in pages or [] for c in page]

    def claim_record(self, number: int, kind: str):
        """One read for checking ACTIONS claims (commonroom#68): the issue or PR, or its timeline events."""
        if kind == "issue":
            return board_mod.gh(["api", f"repos/{self.repo}/issues/{number}"])
        pages = board_mod.gh(["api", "--paginate", "--slurp", f"repos/{self.repo}/issues/{number}/timeline?per_page=100"])
        return [e for page in pages or [] for e in page]

    def open_prs(self) -> list[dict]:
        return board_mod.gh(["pr", "list", "--repo", self.repo, "--state", "open", "--limit", "100",
                             "--json", f"number,headRefName,headRefOid,url,title,labels,{PR_ORIGIN}"]) or []

    def queued_runs(self) -> list[dict]:
        return board_mod.gh(["run", "list", "--repo", self.repo, "--status", "queued", "--limit", "100",
                             "--json", "databaseId,headBranch,headSha,event"]) or []

    def cancel_run(self, run_id: int):
        board_mod.gh(["run", "cancel", str(run_id), "--repo", self.repo])

    def finished_prs(self) -> list[dict]:
        """Merged or closed PRs: `--state closed` lists both (commonroom#108)."""
        return board_mod.gh(["pr", "list", "--repo", self.repo, "--state", "closed", "--limit", "200",
                             "--json", "number"]) or []

    def pr_detail(self, number: int) -> dict:
        return board_mod.gh(["pr", "view", str(number), "--repo", self.repo,
                             "--json", "number,state,headRefName,headRefOid,baseRefName,url,commits,comments,"
                                       f"statusCheckRollup,mergeable,files,labels,createdAt,{PR_ORIGIN}"])

    def behind_main(self, detail: dict) -> int:
        """Commits on the PR's base branch that its head doesn't contain yet."""
        base = detail.get("baseRefName") or "main"

        def behind_from(sha: str) -> int:
            compare = board_mod.gh(["api", f"repos/{self.repo}/compare/{base}...{sha}", "--jq", "{behind_by: .behind_by}"]) or {}
            return int(compare.get("behind_by") or 0)

        behind = behind_from(detail["headRefOid"])
        if behind and detail.get("headRefName"):
            # `pr view` can report an older head than the branch's tip (gamemirage PR #653 read 1 behind for a day after
            # GitHub's update had already merged main). Trust the branch itself: one extra call, only when behind.
            tip = board_mod.gh(["api", f"repos/{self.repo}/git/ref/heads/{detail['headRefName']}", "--jq", "{sha: .object.sha}"]) or {}
            if tip.get("sha") and tip["sha"] != detail["headRefOid"]:
                behind = behind_from(tip["sha"])
        return behind

    def compare_since(self, base: str, head: str) -> dict:
        """What lies between two commits: `commits` is each commit after `base` with its parent shas, `files` the paths
        changed. Raises when GitHub's answer is cut short (it caps commits and files), so no one trusts a partial one."""
        if len(base) < 40:   # a `HEAD:` stamp may be a short sha
            base = (board_mod.gh(["api", f"repos/{self.repo}/commits/{base}", "--jq", "{sha: .sha}"]) or {}).get("sha") or base
        compare = board_mod.gh(["api", f"repos/{self.repo}/compare/{base}...{head}", "--jq",
                                "{total: .total_commits, commits: [.commits[] | {sha: .sha, parents: [.parents[].sha]}], "
                                "files: [(.files // [])[].filename]}"]) or {}
        if compare.get("total") is None or compare["total"] != len(compare.get("commits") or []) or len(compare.get("files") or []) >= 300:
            raise RuntimeError(f"compare {base[:7]}...{head[:7]} is incomplete")
        return compare

    def update_branch(self, number: int):
        """Merge the base branch into the PR's branch on GitHub (no model run). Raises when that conflicts."""
        board_mod.gh(["pr", "update-branch", str(number), "--repo", self.repo])

    def main_checks(self) -> list[dict]:
        """Main's latest check runs, shaped like a PR's statusCheckRollup."""
        runs = board_mod.gh(["api", f"repos/{self.repo}/commits/main/check-runs?per_page=100", "--jq",
                             "[.check_runs[] | {name, conclusion: (.conclusion // .status), detailsUrl: .html_url, sha: .head_sha}]"])
        runs = runs or []
        # Each check's workflow run and that run's attempt, so a red main is rerun once before a fix task opens
        # (commonroom#88). The attempt comes from GitHub, not from memory, so a restarted dispatcher can't loop.
        for c in runs:
            if m := RUN_URL.search(c.get("detailsUrl") or ""):
                c["runUrl"], c["runId"] = m.group(1), m.group(2)
        if any(c.get("runId") for c in runs):
            # A failed run list leaves attempt and status unset: main_red() then opens the fix task, it never merges
            # on a red main it couldn't read (commonroom#98).
            try:
                listed = board_mod.gh(["run", "list", "--repo", self.repo, "--branch", "main", "--limit", "50",
                                       "--json", "databaseId,attempt,status"]) or []
            except Exception:
                listed = []
            known = {str(r.get("databaseId")): r for r in listed}
            for c in runs:
                if c.get("runId") in known:
                    c["attempt"], c["status"] = known[c["runId"]].get("attempt"), known[c["runId"]].get("status")
        return runs

    def rerun_failed(self, run_id: str):
        """Rerun a workflow run's failed jobs. No backoff and no cap here: the caller decides from the run's attempt."""
        board_mod.gh(["run", "rerun", run_id, "--repo", self.repo, "--failed"])

    def create_issue(self, title: str, body: str, labels=(), milestone: str | None = None, assignees=()) -> int:
        args = ["issue", "create", "--repo", self.repo, "--title", title, "--body", body]
        for name in labels:
            args += ["--label", name]
        for login in assignees:
            args += ["--assignee", login]
        if milestone:
            args += ["--milestone", milestone]
        url = board_mod.gh(args)
        return int(str(url).rstrip("/").rsplit("/", 1)[1])

    def pr_for_branch(self, branch: str) -> dict | None:
        """The newest PR from this repository's own `branch`: a fork's PR can carry the same branch name."""
        prs = board_mod.gh(["pr", "list", "--repo", self.repo, "--head", branch, "--state", "all",
                            "--json", f"number,state,url,headRefName,{PR_ORIGIN}"]) or []
        return next((pr for pr in prs if pr.get("isCrossRepository") is False), None)

    def close_pr(self, number: int, body: str):
        """Close a PR unmerged with a room comment. Its branch stays, so nothing on it is lost."""
        board_mod.gh(["pr", "close", str(number), "--repo", self.repo, "--comment", f"{MARK} {body}"])

    def label_pr(self, number: int, name: str):
        board_mod.gh(["pr", "edit", str(number), "--repo", self.repo, "--add-label", name])

    def merge(self, number: int):
        """Squash-merge, then delete the head branch. Main's commit is the PR's title and body, whatever the branch's
        commits say: GitHub's default takes a lone commit's message, which once made the room's WIP placeholder the
        permanent description of a change (commonroom#21). PRs stacked on that branch move to its base first: GitHub
        closes a PR whose base branch is deleted through the API rather than retargeting it (gamemirage#368)."""
        pr = board_mod.gh(["pr", "view", str(number), "--repo", self.repo,
                           "--json", "headRefName,baseRefName,title,body"]) or {}
        args = ["pr", "merge", str(number), "--repo", self.repo, "--squash"]
        if (pr.get("title") or "").strip():
            args += squash_message(number, pr["title"], pr.get("body"))
        board_mod.gh(args)
        head, base = pr.get("headRefName"), pr.get("baseRefName") or "main"
        if not head:
            return
        for child in board_mod.gh(["pr", "list", "--repo", self.repo, "--base", head, "--state", "open",
                                   "--json", "number,isCrossRepository"]) or []:
            if child.get("isCrossRepository"):
                continue   # a fork's PR is never edited by the room (commonroom#176)
            board_mod.gh(["pr", "edit", str(child["number"]), "--repo", self.repo, "--base", base])
        try:
            board_mod.gh(["api", "-X", "DELETE", f"repos/{self.repo}/git/refs/heads/{head}"])
        except RuntimeError:
            pass   # already gone (deleted by hand or by the repo's own setting): the merge is what matters

    def rerun_ci(self, run_id: str, minutes_since: float) -> int | None:
        """Rerun a workflow run's failed or cancelled jobs, backing off during an outage.
        Returns the attempt that was rerun, 0 while backing off, or None once capped."""
        attempt = (board_mod.gh(["run", "view", run_id, "--repo", self.repo, "--json", "attempt"]) or {}).get("attempt", 1)
        if attempt >= CI_RERUNS + 1:
            return None
        if minutes_since < CI_BACKOFF_MINUTES * (attempt - 1):
            return 0
        board_mod.gh(["run", "rerun", run_id, "--repo", self.repo, "--failed"])
        return attempt


def login_of(item: dict | None) -> str:
    """The lower-cased login of whoever wrote an issue, PR or comment; '' when GitHub didn't say."""
    return str(((item or {}).get("author") or {}).get("login") or "").lower()


class TrustedBoard:
    """The board as the room sees it: only what its trusted logins wrote (commonroom#176). The repos are public, so anyone
    can open an issue or a PR, or comment, and the room acts on what it reads: a stranger's `REVIEW: APPROVE` on a
    fork's `i<N>-` PR could have merged code the owner's PC then runs. Issues and comments by anyone else are left out
    of every read, and so are PRs from another repository or by anyone else, which `untrusted_prs` keeps for the
    owner's report. No author means untrusted. Writes and everything else pass straight to the board underneath."""

    def __init__(self, board, logins: set[str], repo_owner: str):
        self.inner, self.logins, self.repo_owner = board, logins, repo_owner.lower()
        self.untrusted_prs: list[dict] = []

    def __getattr__(self, name):
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def trusts(self, item: dict | None) -> bool:
        return login_of(item) in self.logins

    def own_pr(self, pr: dict | None) -> bool:
        """A PR the room may review, update or merge: its head is a branch of this repository, opened by a trusted login."""
        return (bool(pr) and pr.get("isCrossRepository") is False and self.trusts(pr)
                and str((pr.get("headRepositoryOwner") or {}).get("login") or "").lower() == self.repo_owner)

    def keep_trusted_comments(self, item: dict) -> dict:
        if any(not self.trusts(c) for c in item.get("comments") or []):
            item["comments"] = [c for c in item["comments"] if self.trusts(c)]
        return item

    def open_issues(self, milestone: str | None) -> list[dict]:
        return [self.keep_trusted_comments(i) for i in self.inner.open_issues(milestone) or [] if self.trusts(i)]

    def all_issues(self) -> list[dict]:
        return [self.keep_trusted_comments(i) for i in self.inner.all_issues() or [] if self.trusts(i)]

    def issue_comments(self, number: int, since: float) -> list[dict]:
        return [c for c in self.inner.issue_comments(number, since) or [] if self.trusts(c)]

    def open_prs(self) -> list[dict]:
        prs = self.inner.open_prs() or []
        self.untrusted_prs = [pr for pr in prs if not self.own_pr(pr)]
        return [pr for pr in prs if self.own_pr(pr)]

    def pr_detail(self, number: int) -> dict:
        """Only trusted comments, and none on a PR that isn't the room's own, so no verdict on it can count."""
        detail = self.inner.pr_detail(number)
        return self.keep_trusted_comments(detail) if self.own_pr(detail) else detail | {"comments": []}

    def pr_for_branch(self, branch: str) -> dict | None:
        pr = self.inner.pr_for_branch(branch)
        return pr if self.own_pr(pr) else None


SQUASH_BODY_MAX = 4000   # well inside Windows' 32 767-character command line


def squash_message(number: int, title: str, body: str | None) -> list[str]:
    """`gh pr merge` flags that make the squash commit read as the PR: "<title> (#<n>)" like GitHub's own multi-commit
    default, and the PR body, trimmed. `--flag=value` so a body starting with "-" can't be read as a flag."""
    title = " ".join(title.split())
    subject = title if title.endswith(f"(#{number})") else f"{title} (#{number})"
    text = (body or "").replace("\r\n", "\n").strip()
    if len(text) > SQUASH_BODY_MAX:
        cut = text[:SQUASH_BODY_MAX]
        text = cut[:cut.rfind("\n")] if "\n" in cut else cut
        text = text.rstrip() + f"\n\n(Trimmed: the full description is on PR #{number}.)"
    return [f"--subject={subject}", f"--body={text}"]


# PR state ----------------------------------------------------------------------------------------
VERDICT = re.compile(r"\s*(?:\[agent:(?:reviewer|director)\]\s*)?REVIEW:\s*(APPROVE|CHANGES|INCOMPLETE)")


def verdict_of(body: str | None) -> str | None:
    """APPROVE, CHANGES or INCOMPLETE (the reviewer's test run couldn't finish: no defect found, no approval either)
    if the comment is a review verdict, else None. Only a body that starts with `REVIEW:`, or with
    `[agent:reviewer]` or `[agent:director]` and then `REVIEW:`, is one: a verdict quoted in a builder's or the room's
    comment is not (commonroom#58)."""
    m = VERDICT.match(body or "")
    return m.group(1) if m else None


def review_comment(detail: dict) -> dict | None:
    """The comment holding the newest REVIEW verdict posted after the PR's latest commit."""
    commits = detail.get("commits") or []
    last_commit = max((c.get("committedDate") or "" for c in commits), default="")
    found = None
    for c in detail.get("comments") or []:
        if (c.get("createdAt") or "") >= last_commit and verdict_of(c.get("body")):
            found = c
    return found


def latest_review(detail: dict) -> str | None:
    """The newest REVIEW verdict posted after the PR's latest commit, or None if it needs (re)review."""
    c = review_comment(detail)
    return verdict_of(c["body"]) if c else None


INCOMPLETE_ROUNDS = 2   # this many INCOMPLETE verdicts at one head stop re-reviews and go to the director (commonroom#174)


def incomplete_reviews(detail: dict) -> int:
    """INCOMPLETE verdicts posted since the PR's latest commit, i.e. at its current head."""
    commits = detail.get("commits") or []
    last_commit = max((c.get("committedDate") or "" for c in commits), default="")
    return sum(1 for c in detail.get("comments") or []
               if (c.get("createdAt") or "") >= last_commit and verdict_of(c.get("body")) == "INCOMPLETE")


DIRECTOR_MARK = "[agent:director]"
DIRECTOR_LABEL = "needs:director"   # on a PR: it needs lines in another project's room.toml or prompts (commonroom#65)


def director_comment(detail: dict) -> dict | None:
    """The newest director verdict comment posted after the PR's latest commit. Only a comment that starts with
    `[agent:director]` counts, and a later commit voids it (commonroom#49)."""
    commits = detail.get("commits") or []
    last_commit = max((c.get("committedDate") or "" for c in commits), default="")
    found = None
    for c in detail.get("comments") or []:
        body = (c.get("body") or "").strip()
        if verdict_of(body) and body.startswith(DIRECTOR_MARK) and (c.get("createdAt") or "") >= last_commit:
            found = c
    return found


def director_verdict(detail: dict) -> str | None:
    c = director_comment(detail)
    return verdict_of((c.get("body") or "").strip()) if c else None


HEAD_LINE = re.compile(r"^\s*HEAD:\s*([0-9a-f]{7,40})\s*$", re.M | re.I)


def director_approved(detail: dict) -> bool:
    """The director approved this exact head: the approval has a line `HEAD: <sha>`, at least 7 characters and a prefix
    of the PR's head sha. A commit made before the approval but pushed after it predates the comment, so the timestamp
    alone can't void the approval (commonroom#55). No such line, or a stale one, means "not approved yet"."""
    c = director_comment(detail)
    body = (c.get("body") or "").strip() if c else ""
    head = head_sha(detail).lower()
    return (verdict_of(body) == "APPROVE" and bool(head)
            and any(head.startswith(m.group(1).lower()) for m in HEAD_LINE.finditer(body)))


def director_stamps(detail: dict) -> list[str]:
    """The `HEAD:` shas of the director's newest verdict on the PR when it is an APPROVE, whenever it was posted
    (commonroom#168: such a stamp can be carried across merge commits). Else []."""
    newest = None
    for c in detail.get("comments") or []:
        body = (c.get("body") or "").strip()
        if verdict_of(body) and body.startswith(DIRECTOR_MARK):
            newest = body
    if newest is None or verdict_of(newest) != "APPROVE":
        return []
    return [m.group(1).lower() for m in HEAD_LINE.finditer(newest)]


def head_sha(detail: dict) -> str:
    commits = detail.get("commits") or []
    return detail.get("headRefOid") or (commits[-1].get("oid") or "" if commits else "")


TESTS_LINE = re.compile(r"^.*\bTESTS:\s*(PASS|FAIL)\b.*$", re.M | re.I)


INCOMPLETE_LINE = re.compile(r"\bTESTS:\s*INCOMPLETE\b", re.I)   # never a pass, whatever else the comment says


def tested_on_head(body: str, head: str, command: str | None) -> bool:
    """A project without CI merges on its reviewer's word, so the approval must state that the reviewer ran the test
    command on the PR head and it passed: a line `TESTS: PASS <head sha> <command>: <result>`. The sha matters because a
    commit pushed while the review ran can still predate the review comment."""
    matches = list(TESTS_LINE.finditer(body or ""))
    if not head or len(head) < 7 or any(m.group(1).upper() == "FAIL" for m in matches) or INCOMPLETE_LINE.search(body or ""):
        return False
    return any(head[:7].lower() in m.group(0).lower() and (command or "") in m.group(0) for m in matches)


def approved_at(detail: dict) -> str:
    """When the newest REVIEW verdict was posted, if it is an APPROVE (whatever commits followed), else ''."""
    verdict, at = None, ""
    for c in detail.get("comments") or []:
        found = verdict_of(c.get("body"))
        if found:
            verdict, at = found, c.get("createdAt") or ""
    return at if verdict == "APPROVE" else ""


CONFLICT_HUNK = re.compile(r"^<{7}(?: [^\n]*)?\n.*?^>{7}(?: [^\n]*)?(?:\n|\Z)", re.M | re.S)


def resolves_only_conflicts(conflicted: str, resolved: str) -> bool:
    """Does `resolved` keep every line of git's conflicted merge outside the conflict hunks, in order? Then whoever
    resolved it changed nothing but the conflicts."""
    keep = CONFLICT_HUNK.split(conflicted)
    if len(keep) == 1:
        return conflicted == resolved
    head, tail = keep[0], keep[-1]
    if not (resolved.startswith(head) and resolved.endswith(tail)) or len(head) + len(tail) > len(resolved):
        return False
    pos, end = len(head), len(resolved) - len(tail)
    for part in keep[1:-1]:   # leftmost matches leave the most room for the parts after them
        found = resolved.find(part, pos, end)
        if found < 0:
            return False
        pos = found + len(part)
    return True


def merges_main_only(root: Path, oid: str, main: str = "origin/main") -> bool:
    """Is this commit a merge of main into the branch that brings in main and nothing else? Its tree must be git's own
    merge of its parents, or differ from it only inside the conflict hunks (commonroom#14). Any doubt: no."""
    def run(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        parents = run("rev-list", "--parents", "-n", "1", oid).stdout.split()[1:]
        if len(parents) != 2 or run("merge-base", "--is-ancestor", parents[1], main).returncode != 0:
            return False
        merged = run("merge-tree", "--write-tree", "--name-only", "--no-messages", *parents)
        if merged.returncode not in (0, 1):
            return False
        tree, *conflicted = [line for line in merged.stdout.splitlines() if line]
        changed = run("diff", "--name-only", "--no-renames", tree, oid)
        if changed.returncode != 0:
            return False
        for path in changed.stdout.splitlines():
            if path not in conflicted:
                return False
            ours, theirs = run("show", f"{tree}:{path}"), run("show", f"{oid}:{path}")
            if ours.returncode or theirs.returncode or not resolves_only_conflicts(ours.stdout, theirs.stdout):
                return False
        return True
    except (OSError, ValueError):
        return False


def checks_state(detail: dict) -> str:
    """'green', 'pending' or 'red'. No checks at all counts as pending: CI is required."""
    rollup = detail.get("statusCheckRollup") or []
    if not rollup:
        return "pending"
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in rollup]
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED") for s in states):
        return "red"
    if all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in states):
        return "green"
    return "pending"


INFRA_CONCLUSIONS = {"CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "STALE"}
CI_RERUNS = 6          # infrastructure cancellations are rerun at most this many times per run...
CI_BACKOFF_MINUTES = 20  # ...waiting 20, 40, 60... minutes between reruns, so an outage can't use them all up
CI_BOUNCES = 3         # red CI sends a PR back to its builder at most this often before the director steps in
MERGE_MAIN = "merge-main run"   # marks the room's comment that sends a PR back only to merge main into it
CONFLICTS_MAIN = "conflicts with main"   # marks the room's comment that sends a conflicting PR back to its builder
MAIN_RED = "Main is red"        # title prefix of the fix task the room opens when main's own CI fails
RUN_URL = re.compile(r"(.*/actions/runs/(\d+))")   # a check's workflow run, from its details URL


def merge_main_only(issue: dict) -> bool:
    """Did the room's latest word on this issue send it back only to merge main into its PR (commonroom#14)?"""
    room = [c for c in issue.get("comments") or [] if (c.get("body") or "").lstrip().startswith(MARK)]
    return bool(room) and MERGE_MAIN in (room[-1].get("body") or "")


def conflicts_main(issue: dict) -> bool:
    """Did the room's latest word on this issue say its PR conflicts with main (commonroom#51)? Merging main needs
    `git fetch` and a commit, which Codex's sandbox can't do, so such an issue never goes to Codex."""
    room = [c for c in issue.get("comments") or [] if (c.get("body") or "").lstrip().startswith(MARK)]
    return bool(room) and CONFLICTS_MAIN in (room[-1].get("body") or "")


REVIEW_ROUNDS = 2      # this many CHANGES verdicts on one PR go to the director, not back to the builder (commonroom#32)
ROUNDS_ESCALATED = "review rounds without an approval"   # marks the escalation comment; rounds count again after it


def changes_rounds(detail: dict, since: str = "") -> int:
    """REVIEW: CHANGES verdicts on a PR posted after `since` (an ISO time; '' counts all)."""
    return sum(1 for c in detail.get("comments") or []
               if (c.get("createdAt") or "") > since and verdict_of(c.get("body")) == "CHANGES")


def failed_checks(detail: dict) -> list[dict]:
    return [c for c in detail.get("statusCheckRollup") or []
            if (c.get("conclusion") or c.get("state") or "").upper() in ("FAILURE", "ERROR", "ACTION_REQUIRED", *INFRA_CONCLUSIONS)]


def attempt_links(check: dict) -> str:
    """A failed check's link, or one link per attempt of its workflow run once it has been rerun (commonroom#88)."""
    attempt = check.get("attempt") or 1
    if attempt < 2 or not check.get("runUrl"):
        return str(check.get("detailsUrl"))
    return ", ".join(f"attempt {n} {check['runUrl']}/attempts/{n}" for n in range(1, attempt + 1))


def ci_infra_only(detail: dict) -> bool:
    """Red only because CI never really ran (runner outage, timeout, cancellation): rerun, don't blame the builder."""
    failed = failed_checks(detail)
    return bool(failed) and all((c.get("conclusion") or c.get("state") or "").upper() in INFRA_CONCLUSIONS for c in failed)


HEADLESS_NOTE = ("\n\nYou are running headless, in a single turn: the moment you stop, the session ends and nobody wakes you "
                 "again. Never background a command and wait for it, and don't use Monitor. Run commands in the foreground "
                 "(the Bash tool allows up to 600000 ms). Finish your whole job, including the final comment or PR, before you stop. "
                 "The room already checked the budget before starting you: don't run `room.py gate` yourself. Stop only processes you started, by PID.")
CODEX_CANNOT = {"kind:perf"}   # Codex's Windows sandbox can't start Chrome/Edge (gamemirage#121)
ROOM_ROOT = Path(__file__).resolve().parent.parent   # the Common Room checkout this code runs from
ORPHAN_AGE = 7200   # a worker never runs past an hour; a run 'running' for longer has lost its dispatcher


def pid_alive(pid: int) -> bool:
    """Is this dispatcher process still running? Errs towards yes: wrongly closing a live run is worse."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FI", "IMAGENAME eq python.exe", "/NH"],
                                 capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def owner_comments(issue: dict) -> list[dict]:
    """The owner's own words: comments not by an agent or the room. Only trusted logins' comments reach the room at all
    (TrustedBoard), and agents post as the owner, so the `[agent:...]`/`[room]` prefix is what tells them apart."""
    return [c for c in issue.get("comments") or [] if not (c.get("body") or "").lstrip().startswith("[")]


# Policy ------------------------------------------------------------------------------------------
MIN_FREE_GB = {"room": 3, "project": 8}


def disk_verdict(cfg: dict, ledger: Ledger) -> str | None:
    """Hold all work when a drive the room writes to is nearly full. A full system drive took the ledger, the dispatchers
    and a moonshot down at once (2026-10-07). It holds new starts only, so running workers finish."""
    for label, path in (("room", Path(ledger.path).parent), ("project", Path(cfg.get("_root") or "."))):
        try:
            free = shutil.disk_usage(path).free / 2**30
        except OSError:
            continue
        if free < (cfg.get("min_free_gb") or MIN_FREE_GB).get(label, 0):
            return f"disk nearly full: {free:.1f} GB free on {Path(path).anchor} ({label}); clear space before more work"
    return None


def verdicts(ledger: Ledger, cfg: dict, now: float | None = None, trip: bool = False) -> dict[str, gate.Verdict]:
    """Only the dispatcher records breaker trips (`trip=True`). Views (status, dashboard) are read-only, so a
    long-running view on old code can never re-trip a breaker the current code would not."""
    now = time.time() if now is None else now
    project = cfg["project"].get("name")
    n = cfg["breakers"]["no_output_runs"]
    result = {}
    disk = disk_verdict(cfg, ledger)
    for provider in ("claude", "codex"):
        # A provider that keeps producing nothing is held on its own; the other keeps working.
        # Breakers named `kind:provider` hold that provider only; owner/auditor pauses and no_progress hold everything.
        runs = ledger.recent_runs(project, limit=n, autonomous=True, finished=True, provider=provider)
        mine = [b for b in ledger.open_breakers(project) if ":" not in b["kind"] or b["kind"].endswith(":" + provider)]
        # A guest also stops when its host's room-wide stops are open (a pause, no_progress). The host's provider
        # breakers judge the host's own runs; the guest's runs have breakers of their own.
        mine += [b for b in ledger.open_breakers(cfg["_host"]) if ":" not in b["kind"]] if cfg.get("_host") else []
        breaker, tripped = gate.breaker_verdict(mine, runs, n)
        if disk:
            result[provider] = breaker if mine and breaker.stop_running else gate.Verdict(False, disk)
            continue
        if tripped and trip:
            ledger.trip(project, f"{tripped['kind']}:{provider}", tripped["detail"], now=now)
        v = gate.usage_verdict(provider, ledger.latest_usage(provider), config_mod.budget(cfg, provider), now,
                               cfg["freshness_seconds"])
        cool = ledger.cooldown(provider, now)
        if v.allowed and cool:
            v = gate.Verdict(False, f"{provider}: {cool['reason']}; resting until "
                                    f"{time.strftime('%H:%M', time.localtime(cool['until']))}", v.detail)
        if provider == "claude" and v.allowed:
            owner = cfg.get("owner") or {}
            minutes = owner.get("active_minutes", 20)
            elsewhere = usage.owner_active(cfg.get("_root"), minutes, now=now,
                                           exclude=ledger.worker_dirs(now - minutes * 60))
            if elsewhere:
                # The owner shares this plan: yield to their own sessions. Running workers finish; nothing new
                # starts once the 5-hour window passes the cap.
                v.detail["owner_active"] = elsewhere
                cap = owner.get("new_work_cap_5h", 70)
                if v.detail.get("five_hour_used", 0) >= cap:
                    v = gate.Verdict(False, f"claude: the owner is using Claude elsewhere; no new work above {cap}% "
                                            f"of the 5-hour window", v.detail)
        # A breaker shows when nothing else holds; one that stops running workers outranks every other hold.
        if not breaker.allowed and (v.allowed or (breaker.stop_running and not v.stop_running)):
            v = gate.Verdict(False, breaker.reason, v.detail, breaker.stop_running)
        result[provider] = v
    return result


def choose_provider(spec: list[str], allowed: dict[str, gate.Verdict], exclude: str | None = None) -> tuple[str, str | None] | None:
    """Role spec like ["codex", "claude:sonnet"] -> first allowed (provider, model)."""
    for entry in spec:
        provider, _, model = entry.partition(":")
        if provider == exclude:
            continue
        if provider in allowed and allowed[provider].allowed:
            return provider, model or None
    return None


def run_minutes(ledger: Ledger, provider: str, since: float, now: float) -> dict[str | None, float]:
    """Autonomous model-run minutes per project on one provider, for runs started since `since` (running runs count
    up to now). Every project on the ledger shares the plan, so this is how the plan's time is split between them."""
    minutes: dict[str | None, float] = {}
    for r in ledger.recent_runs(None, limit=5000, autonomous=True, provider=provider):
        if r["started_at"] < since:
            break   # newest first
        minutes[r["project"]] = minutes.get(r["project"], 0.0) + max(0.0, (r["ended_at"] or now) - r["started_at"]) / 60
    return minutes


def share_verdict(ledger: Ledger, cfg: dict, provider: str, now: float) -> gate.Verdict | None:
    """A guest project (`[share] max`) may start model work while its share of all projects' autonomous run time on
    this provider over the trailing `hours` is under `max`, or while no other project has used the provider for
    `idle_minutes` (nobody is waiting on that allowance). None means no objection; housekeeping is never held."""
    share = cfg.get("share") or {}
    if share.get("max") is None:
        return None
    project, hours = cfg["project"].get("name"), float(share.get("hours", 24))
    idle = float(share.get("idle_minutes", 60))
    others_last = max((r["ended_at"] or now for r in ledger.recent_runs(None, limit=500, autonomous=True, provider=provider)
                       if r["project"] != project), default=0.0)
    if idle and now - others_last >= idle * 60:
        return None
    minutes = run_minutes(ledger, provider, now - hours * 3600, now)
    total, mine = sum(minutes.values()), minutes.get(project, 0.0)
    if total <= 0 or mine / total < float(share["max"]):
        return None
    host = cfg.get("_host") or "the other projects"
    return gate.Verdict(False, f"{provider}: {project} has had its share ({mine / total:.0%} of autonomous {provider} time in "
                               f"{hours:g}h, cap {float(share['max']):.0%}); {host} goes first")


def check_progress(ledger: Ledger, project: str | None, now: float, spend_limit: float = 10.0,
                   attribute: bool = False) -> dict | None:
    """Progress guarantee: >spend_limit weekly-% of autonomous Claude spend in 24h with no merge trips a breaker.
    With `attribute` (a guest sharing the plan), only the project's share of Claude run time counts as its spend."""
    since = now - 86400
    runs = [r for r in ledger.recent_runs(project, limit=500) if r["started_at"] >= since]
    if not any(r["provider"] == "claude" and r["autonomous"] and r["role"] != "merge" for r in runs):
        return None   # the room didn't work: whatever was spent was the owner's own use
    # Plan-wide weekly growth over 24h (per-run deltas double-count parallel runs). Within each weekly period
    # take last minus first, so a reset or a flapping last digit can't inflate it.
    periods: dict = {}
    for r in ledger.usage_history("claude", "seven_day", since):
        key = round((r["resets_at"] or 0) / 3600)
        periods.setdefault(key, [r["used_pct"], r["used_pct"]])[1] = r["used_pct"]
    spent = sum(max(0.0, last - first) for first, last in periods.values())
    if attribute:
        minutes = run_minutes(ledger, "claude", since, now)
        spent *= minutes.get(project, 0.0) / (sum(minutes.values()) or 1.0)
    merged = sum(1 for r in runs if r["role"] == "merge")
    if spent > spend_limit and merged == 0:
        return {"kind": "no_progress", "detail": f"{spent:.1f}% of the weekly Claude allowance spent in 24h of room work with nothing merged"}
    return None


# The dispatcher ----------------------------------------------------------------------------------
RESUME_WINDOW = 12 * 3600   # a stopped Claude session older than this starts fresh (commonroom#142)


def session_file(worktree, session_id: str, projects_dir: Path | None = None) -> Path:
    """Where Claude Code keeps a session started in `worktree`: <projects>/<path, non-alphanumerics as '-'>/<id>.jsonl."""
    root = projects_dir or Path.home() / ".claude" / "projects"
    return root / usage.session_folder(worktree) / f"{session_id}.jsonl"


def orphaned(run: dict, now: float) -> bool:
    """A 'running' row whose dispatcher is gone: older than ORPHAN_AGE, or its pid (not ours) is dead."""
    dead = run.get("pid") and run["pid"] != os.getpid() and not pid_alive(run["pid"])
    return bool(dead) or now - run["started_at"] >= ORPHAN_AGE


@dataclass
class Job:
    kind: str           # build | review | direct
    role: str
    issue: dict | None = None
    pr: dict | None = None
    reason: str = ""


class Dispatcher:
    def __init__(self, cfg: dict, ledger: Ledger, board=None, log=print, dry_run: bool = False, only_provider: str | None = None,
                 slot: int = 1, notifier=None):
        self.cfg, self.ledger, self.log, self.dry_run = cfg, ledger, log, dry_run
        self.claude_projects = Path.home() / ".claude" / "projects"
        self.slot = slot  # slot 2+ is an extra parallel worker for the same provider (builds only)
        # With one dispatcher per provider, each takes only jobs whose policy choice is its own provider.
        self.only_provider = only_provider
        self.project = cfg["project"].get("name")
        self.root = Path(cfg["_root"]) if cfg.get("_root") else None
        self.board = board or GitHubBoard(cfg["project"]["repo"])
        self.milestone = cfg["goal"].get("milestone")
        self.roles = cfg.get("roles") or {}
        self.log_dir = Path(ledger.path).parent / "logs" / (self.project or "default")
        self._provider = None
        self.ci = config_mod.has_ci(cfg)
        # Globs of repo files whose PRs also need the director's approval to merge (commonroom#49). Empty: no extra gate.
        self.director_paths = list((cfg.get("merge") or {}).get("director_paths") or [])
        self.carry_cache = {}   # (stamp sha, head sha) -> may the stamp be carried; one compare per pair per pass
        self.room_root = ROOM_ROOT   # the checkout this code runs from; tests point it elsewhere
        # Pushes owner notifications to a phone (`[notify]`, commonroom#151). None: nothing is sent.
        self.notifier = notifier if notifier is not None else notify_mod.from_config(cfg)
        self.owner = config_mod.owner(cfg)

    @property
    def board(self) -> TrustedBoard:
        return self._board

    @board.setter
    def board(self, board):
        """Whatever board the room is given, it reads it through TrustedBoard: only its trusted logins' work counts."""
        repo_owner = str(self.cfg["project"].get("repo") or "").split("/", 1)[0]
        self._board = board if isinstance(board, TrustedBoard) else TrustedBoard(board, config_mod.trusted(self.cfg), repo_owner)

    def worktree_base(self) -> Path:
        """Where worker worktrees go: `[project] worktrees`, or `<project>-wt/room` next to the project. Not the
        project's .claude/worktrees: Claude Code creates and removes its own worktrees there (commonroom#48)."""
        configured = self.cfg["project"].get("worktrees")
        return Path(configured) if configured else self.root.parent / f"{self.root.name}-wt" / "room"

    def worktree_bases(self) -> list[Path]:
        """Where the room's worktrees may be: the base, then the project's .claude/worktrees, the default before
        commonroom#48, so runs live at the switch are still recovered. Only room-named folders are looked for there."""
        legacy = self.root / ".claude" / "worktrees"
        return [self.worktree_base()] + ([legacy] if not self.cfg["project"].get("worktrees") else [])

    def allowed(self, now: float) -> dict[str, gate.Verdict]:
        """The shared budget gate and breakers, then this project's share of the plan if it is a guest."""
        result = verdicts(self.ledger, self.cfg, now, trip=True)
        for provider, v in result.items():
            held = share_verdict(self.ledger, self.cfg, provider, now) if v.allowed else None
            if held:
                result[provider] = gate.Verdict(False, held.reason, v.detail)
        return result

    # Housekeeping (no model) -----------------------------------------------------------------------
    def housekeeping(self) -> list[str]:
        actions = []
        self.carry_cache = {}
        everything = self.board.all_issues()
        state = {i["number"]: i["state"] for i in everything}
        prs = self.board.open_prs()
        actions += self.report_untrusted_prs(self.board.untrusted_prs)
        # An issue in review whose PR was closed unmerged (e.g. a stacked base was deleted) has nothing to review:
        # send it back to its builder, who continues from the preserved branch (gamemirage#278).
        has_pr = {int(m.group(1)) for pr in prs if (m := BRANCH.match(pr["headRefName"]))}
        running = self.ledger.running_tasks(self.project)
        actions += self.clear_stray_ready(everything)
        for issue in everything:
            n = issue["number"]
            if (issue["state"] == "OPEN" and "status:review" in labels(issue) and n not in has_pr
                    and EXTERNAL not in labels(issue) and f"#{n}" not in running and not self.dry_run):
                self.board.set_labels(n, add=["status:ready"], remove=["status:review"])
                self.board.comment(n, "[room] This issue was in review but has no open PR (it was closed without merging). "
                                      "Back to ready: the next builder continues from the issue's branch and opens a new PR.")
                actions.append(f"#{n} was in review with no open PR; back to ready")
        for claim in stale_claims(everything, prs, self.ledger.running_runs(self.project), time.time()):
            n = claim["number"]
            if not self.dry_run:
                self.board.set_labels(n, add=["status:ready"], remove=["status:in-progress"])
                self.board.comment(n, f"Released a stale claim: no room or moonshot run is working on this and it hasn't "
                                      f"changed for {claim['minutes']} min. Back to ready.")
            actions.append(f"released stale claim #{n}")
        merged = updated = False
        # Is main red? Read once per pass, mergeable PR or not, so a red main is rerun or gets its fix task within one
        # heartbeat (commonroom#111: gamemirage's main sat red 1 h 42 min unseen until a PR was approved). Without CI
        # there are no checks to read; it is then read only when a PR is first ready to merge, as before.
        held = self.main_red(everything, actions) if self.ci and not self.dry_run else None
        for pr in sorted(prs, key=lambda p: p["number"]):   # oldest first: a stacked base lands before what builds on it
            m = BRANCH.match(pr["headRefName"])
            # PRs on other branches (audit-*, director-*, moonshot/*) have no issue to send back to, but `review:skip`
            # promises a merge once CI is green, whatever the branch (commonroom#22). Others are their author's to land.
            if not m and "review:skip" not in labels(pr):
                continue
            detail = self.board.pr_detail(pr["number"])
            review, checks = self.review_verdict(self.reviewer_view(detail)), checks_state(detail)
            issue_no = int(m.group(1)) if m else None
            # The director's CHANGES on a PR that needs them sends it back to the builder, as a reviewer's does.
            if self.director_rejected(detail):
                review = "CHANGES"
            # A PR that conflicts with main can't merge (and CI may never run): send it back to its builder.
            if detail.get("mergeable") == "CONFLICTING" and not self.dry_run:
                closed = self.close_if_superseded(pr, detail, issue_no, review)
                if closed:
                    actions += closed
                    if issue_no:
                        state[issue_no] = "CLOSED"
                    continue
                actions += self.back_to_merge_main(pr, issue_no, everything, review)
                continue
            # Red CI: rerun what never really ran; send real failures back to the builder (never let a PR sit red).
            if self.ci and checks == "red" and not self.dry_run:
                actions += self.handle_red_ci(pr, detail, issue_no, everything)
                continue
            # Research reports (docs only) skip review: CI green is enough.
            if self.ci and "review:skip" in labels(pr) and checks == "green":
                review = "APPROVE"
            if self.ci:
                mergeable = review == "APPROVE" and checks == "green"
            else:
                # No CI: the reviewer's approval is the test, so it must state a passing run on this exact head. A
                # branch the room brings up to date with main gets a new head, so the merged tree is reviewed again.
                mergeable = review == "APPROVE" and self.tested(detail)
            if mergeable:
                if not self.dry_run:
                    # One merge per pass, and only of a branch that already contains main, so the CI that passed ran
                    # on exactly the tree main becomes. Two green PRs merged 5 s apart broke main (commonroom#19).
                    if merged:
                        continue
                    if held is None:
                        held = self.main_red(everything, actions)
                    if held and not any(i["number"] == issue_no and i["title"].startswith(MAIN_RED) for i in everything):
                        continue   # only the fix for main lands while main is red
                    try:
                        behind = self.board.behind_main(detail)
                    except Exception as exc:
                        actions.append(f"PR #{pr['number']}: couldn't compare it with main, not merged: {str(exc)[:100]}")
                        continue
                    if behind:
                        closed = self.close_if_superseded(pr, detail, issue_no, review)
                        if closed:
                            actions += closed
                            if issue_no:
                                state[issue_no] = "CLOSED"
                            continue
                        # Updated at this very head on an earlier pass and still behind: GitHub's update did nothing
                        # (commonroom#133). Don't repeat it or hold the slot: a merge-main run pushes a real merge commit.
                        head = head_sha(detail)
                        if self.already_updated(pr["number"], head):
                            actions += self.back_to_merge_main(pr, issue_no, everything, review)
                            continue
                        if not updated:   # one branch at a time: each update reruns CI
                            updated = True
                            try:
                                self.board.update_branch(pr["number"])
                                self.record_update(issue_no, pr["number"], head)
                                actions.append(f"PR #{pr['number']} was {behind} commit(s) behind main: merged main into it "
                                               f"on GitHub; it merges once CI passes on the result")
                            except Exception as exc:
                                if "conflict" in str(exc).lower():
                                    actions += self.back_to_merge_main(pr, issue_no, everything, review)
                                else:
                                    actions.append(f"PR #{pr['number']}: updating it with main failed: {str(exc)[:100]}")
                        continue
                # Asked only once the branch contains main, so the director judges the tree main becomes and an update
                # doesn't void their approval (commonroom#49).
                if self.waits_for_director(detail):
                    actions.append(self.director_wait(pr["number"], detail))
                    continue
                if not self.dry_run:
                    try:
                        self.board.merge(pr["number"])
                    except Exception as exc:  # e.g. another dispatcher merged it a moment ago
                        actions.append(f"merge of PR #{pr['number']} skipped: {str(exc)[:120]}")
                        continue
                    merged = True
                    run = self.ledger.start_run(self.project, f"#{issue_no}" if issue_no else f"PR #{pr['number']}", "merge", "none")
                    output = {"kind": "merge", "ref": f"PR #{pr['number']}"}
                    quiet = self.quiet_merge(detail, issue_no)
                    if quiet:
                        output["quiet"] = quiet
                    self.ledger.finish_run(run, "done", outputs=[output])
                actions.append(f"merged PR #{pr['number']} ({f'issue #{issue_no}' if issue_no else pr['headRefName']})")
                carried = self.carried_stamp(detail) if self.needs_director(detail) else ""
                if carried:
                    actions.append(f"director stamp {carried[:7]} carried to {head_sha(detail)[:7]}: director paths unchanged")
                if not self.dry_run:
                    actions += self.playtest_after_merge(pr, detail, everything)
                if issue_no:
                    state[issue_no] = "CLOSED"
            elif review == "INCOMPLETE" and not self.dry_run:
                # The reviewer's test run couldn't finish: no verdict, so no bounce. Review again, until it keeps
                # happening at one head (commonroom#174).
                view = self.reviewer_view(detail)
                rounds = incomplete_reviews(view)
                issue = next((i for i in everything if i["number"] == issue_no), None)
                if (rounds >= INCOMPLETE_ROUNDS and issue and issue["state"] == "OPEN"
                        and not labels(issue) & {"status:blocked", "needs:director"}):
                    reasons = [line.strip() for c in view.get("comments") or [] if verdict_of(c.get("body")) == "INCOMPLETE"
                               for line in (c.get("body") or "").splitlines() if INCOMPLETE_LINE.search(line)]
                    self.board.set_labels(issue_no, add=["status:blocked", "needs:director"],
                                          remove=["status:review", "status:in-progress", "needs:changes"])
                    self.board.comment(issue_no, f"PR #{pr['number']}'s reviewer couldn't finish the test run {rounds} times at "
                                                 f"the same head, so the room stops re-reviewing it. The director decides: "
                                                 f"run the tests, or fix what stops them.\n\n"
                                                 + "\n".join(f"- {r[:300]}" for r in reasons[-rounds:]))
                    actions.append(f"PR #{pr['number']} review INCOMPLETE {rounds} times; #{issue_no} escalated")
            elif review == "CHANGES" and not self.dry_run:
                issue = next((i for i in everything if i["number"] == issue_no), None)
                # Relabelled ready by hand or script without needs:changes: it would rank as fresh work (commonroom#170).
                if issue and "status:ready" in labels(issue) and "needs:changes" not in labels(issue):
                    self.board.set_labels(issue_no, add=["needs:changes"])
                    issue["labels"] = list(issue.get("labels") or []) + [{"name": "needs:changes"}]
                    actions.append(f"issue #{issue_no}: re-added needs:changes (PR #{pr['number']}'s last review is CHANGES)")
                    continue
                if not issue or labels(issue) & {"status:ready", "status:blocked", "needs:director", EXTERNAL}:
                    continue
                # A builder already working on the CHANGES: the verdict stays current until it pushes, and bouncing
                # the issue now would put status:ready on it mid-build (commonroom#93).
                if f"#{issue_no}" in running:
                    continue
                # A PR that keeps failing review won't pass on the next round either (gamemirage PR #369 went 6+):
                # the director merges it, revises the bar or splits the issue. Rounds count again after an escalation.
                escalated = max((c.get("createdAt") or "" for c in issue.get("comments") or []
                                 if ROUNDS_ESCALATED in (c.get("body") or "")), default="")
                rounds = changes_rounds(detail, since=escalated)
                if rounds >= REVIEW_ROUNDS:
                    self.board.set_labels(issue_no, add=["status:blocked", "needs:director"],
                                          remove=["status:review", "status:in-progress", "needs:changes"])
                    self.board.comment(issue_no, f"PR #{pr['number']} has had {rounds} {ROUNDS_ESCALATED}. Escalating to "
                                                 f"the director instead of another builder round: merge it, revise the bar, "
                                                 f"or split the issue.")
                    actions.append(f"PR #{pr['number']} got CHANGES {rounds} times; #{issue_no} escalated")
                    continue
                self.board.set_labels(issue_no, add=["status:ready", "needs:changes"], remove=["status:review", "status:in-progress"])
                actions.append(f"PR #{pr['number']} needs changes; issue #{issue_no} back to ready")
        if any(a.startswith("merged") for a in actions) and not self.dry_run:
            actions += self.deploy()
            actions += self.update_live_checkout()
        self.count_finished_prs(everything, prs, state)
        for issue in everything:
            names = labels(issue)
            if issue["state"] != "OPEN" or "status:blocked" not in names or names & {"needs:director", "owner:human"}:
                continue
            deps = dependencies(issue.get("body"))
            if deps and all(state.get(d) == "CLOSED" for d in deps):
                if not self.dry_run:
                    self.board.set_labels(issue["number"], add=["status:ready"], remove=["status:blocked"])
                actions.append(f"unblocked #{issue['number']} (dependencies {sorted(deps)} closed)")
        actions += self.mark_unstatused_top(everything, state)
        actions += self.file_unmilestoned_ready(everything)
        if self.ci and not self.dry_run:
            actions += self.cancel_stale_ci_runs()
        actions += self.open_scheduled(everything)
        actions += self.notify_owner(everything)
        actions += self.prune_logs()
        return actions

    def report_untrusted_prs(self, prs: list[dict]) -> list[str]:
        """Open PRs the room never reviews, updates or merges (commonroom#176): from another repository, or opened by a
        login it doesn't trust. Each is reported once, in the housekeeping log and to the owner's phone."""
        actions = []
        for pr in prs:
            key = f"untrusted-pr:{pr['number']}"
            if self.ledger.marked(self.project, key) is not None:
                continue
            head = f"{(pr.get('headRepositoryOwner') or {}).get('login') or '?'}:{pr.get('headRefName') or '?'}"
            line = (f"PR #{pr['number']} ({head}, by {login_of(pr) or 'unknown'}) is from another repository or a login "
                    f"the room doesn't trust: the room won't review, update or merge it; it's the owner's to look at")
            if not self.dry_run:
                self.ledger.mark(self.project, key)
                self.tell_owner(f"{self.cfg['project'].get('name') or ''}: PR #{pr['number']} needs you", line, url=pr.get("url"))
            actions.append(line)
        return actions

    def prune_logs(self, now: float | None = None) -> list[str]:
        """Delete worker logs older than `[logs] keep_days` (default 14) from the room's log folder and this project's
        `logs/<project>` folder (where its worker runs write), at most once a day (commonroom#162). Only files directly
        in those folders, no recursion; at least a day is kept, so a running worker's log is safe."""
        now = time.time() if now is None else now
        key = "prune-logs"
        last = self.ledger.marked(self.project, key)
        if last is not None and now - last < 24 * 3600:
            return []
        try:
            keep_days = max(1.0, float((self.cfg.get("logs") or {}).get("keep_days", 14)))
        except (TypeError, ValueError):
            keep_days = 14.0
        count = size = 0
        for folder in (Path(self.ledger.path).parent / "logs", self.log_dir):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                try:
                    if not path.is_file() or path.is_symlink() or path.stat().st_mtime >= now - keep_days * 86400:
                        continue
                    nbytes = path.stat().st_size
                    if not self.dry_run:
                        path.unlink()
                    count += 1
                    size += nbytes
                except OSError:
                    continue
        if not self.dry_run:
            self.ledger.mark(self.project, key, now)
        if not count:
            return []
        verb = "would delete" if self.dry_run else "deleted"
        return [f"{verb} {count} worker log(s) older than {keep_days:g} days, {size / 1e6:.1f} MB"]

    def open_scheduled(self, everything: list[dict], now: float | None = None) -> list[str]:
        """Each due `[schedule.<role>]` opens a `role:<role>` + `status:ready` issue (plus its `labels`) in the goal
        milestone (commonroom#151). While the last one it opened is still open, none opens: the role hasn't done it yet."""
        now = time.time() if now is None else now
        actions = []
        runnable = runnable_roles(self.cfg) | {"builder", "scout"}
        for role, spec in sorted((self.cfg.get("schedule") or {}).items()):
            key = f"schedule:{role}"
            if not isinstance(spec, dict) or not schedule_due(spec, self.ledger.marked(self.project, key), now):
                continue
            mark = SCHEDULE_MARK.format(role=role)
            if any(i["state"] == "OPEN" and mark in (i.get("body") or "") for i in everything):
                continue
            if role not in runnable:
                actions.append(f"[schedule.{role}] is due, but {role} has no [roles] entry or agent file: nothing opened")
                continue
            title = fill(str(spec.get("title") or f"{role.capitalize()} {{date}}"), now)
            body = fill(str(spec.get("body") or f"Scheduled {role} run for {{date}}. Follow your {role} instructions."), now)
            body = f"{MARK} {body}\n\n{mark}"
            names = [f"role:{role}", "status:ready", *[str(l) for l in spec.get("labels") or []]]
            if self.dry_run:
                actions.append(f"[schedule.{role}] would open: {title}")
                continue
            try:
                for name in names:   # `issue create` fails on a label the repo doesn't have
                    self.board.ensure_label(name)
                number = self.board.create_issue(title, body, labels=names, milestone=self.milestone)
            except Exception as exc:   # a failed board call is reported and tried again next pass
                actions.append(f"[schedule.{role}] could not open its issue: {str(exc)[:100]}")
                continue
            self.ledger.mark(self.project, key, now)
            everything.append({"number": number, "title": title, "body": body, "state": "OPEN", "comments": [],
                               "labels": [{"name": l} for l in names],
                               "milestone": {"title": self.milestone} if self.milestone else None})
            actions.append(f"[schedule.{role}] opened #{number}: {title}")
        return actions

    def issue_url(self, number: int) -> str:
        return f"https://github.com/{self.cfg['project']['repo']}/issues/{number}"

    def tell_owner(self, title: str, message: str, url: str | None = None, priority: int | None = None) -> bool:
        """Push one notification to the owner's phone. False when there's no notifier or it failed; never raises."""
        if not self.notifier or self.dry_run:
            return False
        try:
            self.notifier.send(title, message, url=url, priority=priority)
            return True
        except Exception as exc:
            self.log(f"   owner notification failed: {type(exc).__name__}: {str(exc)[:120]}")
            return False

    def notify_owner(self, everything: list[dict]) -> list[str]:
        """The room posts as the owner's own account, so GitHub never tells the owner about its @mentions or
        assignments (commonroom#151). With a notifier, each pass pushes new open `owner:human` issues, then new comments
        that mention @<owner> (the bulletin among them). The first pass only sets the comment watermark."""
        if not self.notifier or self.dry_run:
            return []
        actions = []
        project = self.cfg["project"].get("name") or ""
        tasks = [i for i in everything if i["state"] == "OPEN" and "owner:human" in labels(i)
                 and self.ledger.marked(self.project, f"owner-task:{i['number']}") is None]
        if tasks:
            lines = "\n".join(f"#{i['number']} {i.get('title') or ''}" for i in tasks)
            title = f"{project}: {tasks[0].get('title')}" if len(tasks) == 1 else f"{project}: {len(tasks)} new tasks for you"
            url = (self.issue_url(tasks[0]["number"]) if len(tasks) == 1 else
                   f"https://github.com/{self.cfg['project']['repo']}/issues?q=is%3Aopen+label%3Aowner%3Ahuman")
            if self.tell_owner(title, lines, url=url):
                for i in tasks:
                    self.ledger.mark(self.project, f"owner-task:{i['number']}")
                actions.append("told the owner about " + ", ".join(f"#{i['number']}" for i in tasks))
        since = self.ledger.marked(self.project, "mentions")
        mention = re.compile(rf"(?<![\w/-])@{re.escape(self.owner)}\b", re.I) if self.owner else None
        newest, found = since or 0.0, []
        for issue in everything:
            for c in issue.get("comments") or []:
                at = _stamp(c.get("createdAt"))
                newest = max(newest, at)
                if since is not None and at > since and mention and mention.search(c.get("body") or ""):
                    found.append((at, issue, c))
        found.sort(key=lambda f: f[0])
        for at, issue, c in found[:NOTIFY_MAX]:
            if not self.tell_owner(f"{project} #{issue['number']}: {issue.get('title') or ''}", (c.get("body") or "").strip(),
                                   url=c.get("url") or self.issue_url(issue["number"])):
                return actions   # the watermark stays, so the rest are sent next pass
            actions.append(f"told the owner about a mention on #{issue['number']}")
        rest = found[NOTIFY_MAX:]
        if rest:
            self.tell_owner(f"{project}: {len(rest)} more mentions",
                            "\n".join(sorted({f"#{f[1]['number']} {f[1].get('title') or ''}" for f in rest})))
        if newest and newest != since:
            self.ledger.mark(self.project, "mentions", newest)
        return actions

    def cancel_stale_ci_runs(self) -> list[str]:
        """Cancel queued PR runs for merged/closed branches and superseded heads: headless agents can't `gh run cancel`
        (gamemirage#376) and one runner makes each stale run ~23 min of delay (commonroom#138). Never stops a pass."""
        actions = []
        try:
            # Untrusted PRs count as open here too: their runs are cancelled only when superseded, as before.
            prs = self.board.open_prs()
            stale = stale_ci_runs(self.board.queued_runs(), prs + self.board.untrusted_prs)
        except Exception as exc:
            return [f"couldn't list queued CI runs: {str(exc)[:100]}"]
        for run_id, reason in stale:
            try:
                self.board.cancel_run(run_id)
                actions.append(f"cancelled queued CI {reason}")
            except Exception as exc:
                actions.append(f"couldn't cancel queued CI {reason}: {str(exc)[:100]}")
        return actions

    def count_finished_prs(self, everything: list[dict], open_prs: list[dict], state: dict[int, str]):
        """"Depends on #517" where #517 is a PR: issues and PRs share numbers, so a number that is no issue and no open PR
        is a merged or closed one, and satisfies the dependency like a closed issue (gamemirage#522 sat blocked 10 h:
        commonroom#108). An open PR keeps its dependants blocked. Marks such numbers CLOSED in `state`."""
        open_numbers = {pr["number"] for pr in open_prs}
        wanted = {d for i in everything if i["state"] == "OPEN" and "status:blocked" in labels(i)
                  for d in dependencies(i.get("body"))} - state.keys() - open_numbers
        if wanted:
            for pr in self.board.finished_prs():
                if pr["number"] in wanted:
                    state[pr["number"]] = "CLOSED"

    def clear_stray_ready(self, everything: list[dict]) -> list[str]:
        """status:ready on an issue that is also in review or in progress invites a second builder: remove it, here
        and in `everything`, so the rest of this pass sees the true state (commonroom#93)."""
        actions = []
        for issue in everything:
            names, n = labels(issue), issue["number"]
            if issue["state"] != "OPEN" or "status:ready" not in names or not names & {"status:review", "status:in-progress"}:
                continue
            if not self.dry_run:
                self.board.set_labels(n, remove=["status:ready"])
                issue["labels"] = [l for l in issue.get("labels", []) if l["name"] != "status:ready"]
            other = "review" if "status:review" in names else "in-progress"
            actions.append(f"#{n} had status:ready while {other}: removed status:ready")
        return actions

    def to_review(self, n: int):
        """Every move to status:review drops the other statuses and needs:changes in the same call (commonroom#93)."""
        self.board.set_labels(n, add=["status:review"], remove=list(LEAVES_REVIEW))

    def mark_unstatused_top(self, everything: list[dict], state: dict[int, str]) -> list[str]:
        """A prio:top issue raised without a status label is invisible to the dispatcher, which only picks
        status:ready: gamemirage#444, the owner's top fix, sat unseen for 2.5 h (commonroom#92). Mark it ready, or
        blocked while a dependency is open."""
        actions = []
        for issue in everything:
            names, n = labels(issue), issue["number"]
            if (issue["state"] != "OPEN" or "prio:top" not in names or not any(l.startswith("role:") for l in names)
                    or any(l.startswith("status:") for l in names) or names & {"owner:human", "needs:director", "parked", EXTERNAL}
                    or (self.milestone and (issue.get("milestone") or {}).get("title") != self.milestone)):
                continue
            status = "status:blocked" if any(state.get(d) != "CLOSED" for d in dependencies(issue.get("body"))) else "status:ready"
            if not self.dry_run:
                self.board.set_labels(n, add=[status])
                self.board.comment(n, f"[room] #{n} is prio:top but had no status label, so nothing could pick it up. "
                                      f"Marked {status}.")
            actions.append(f"#{n} is prio:top with no status: marked {status.split(':')[1]}")
        return actions

    def file_unmilestoned_ready(self, everything: list[dict]) -> list[str]:
        """A ready issue with no milestone is invisible to the dispatcher, which picks only from the goal milestone:
        #93 sat unseen for 8.5 h (commonroom#100). File it in the goal milestone. Other milestones are left alone."""
        actions = []
        if not self.milestone:
            return actions
        for issue in everything:
            names, n = labels(issue), issue["number"]
            if (issue["state"] != "OPEN" or "status:ready" not in names or not any(l.startswith("role:") for l in names)
                    or names & {"owner:human", "needs:director", "parked"} or issue.get("milestone")):
                continue
            if not self.dry_run:
                self.board.set_milestone(n, self.milestone)
                self.board.comment(n, f"[room] #{n} was ready with no milestone, so nothing could pick it up. "
                                      f"Filed it in {self.milestone}.")
                issue["milestone"] = {"title": self.milestone}
            actions.append(f"#{n} was ready with no milestone: filed in {self.milestone}")
        return actions

    def review_verdict(self, detail: dict) -> str | None:
        """latest_review, except that an APPROVE survives later commits that only merge main in: the room's own branch
        updates and conflict-only merge-main runs need no re-review (commonroom#14). CI still has to pass on them."""
        review = latest_review(detail)
        since = approved_at(detail) if review is None else ""
        later = [c.get("oid") for c in detail.get("commits") or [] if (c.get("committedDate") or "") > since]
        if since and later and all(later) and self.only_merges_main(detail.get("headRefName") or "", later):
            return "APPROVE"
        return review

    def only_merges_main(self, branch: str, oids: list[str]) -> bool:
        if not self.root or not branch:
            return False
        try:
            git(["fetch", "--quiet", "origin", "main", branch], self.root)
        except RuntimeError:
            return False
        return all(merges_main_only(self.root, oid) for oid in oids)

    def own_diff_empty(self, detail: dict) -> bool:
        """Is everything this PR changes already on main? `git diff origin/main origin/<branch> -- <its files>` is empty
        once another PR built on top of it has merged (gamemirage #238 after #255). Any doubt: no."""
        branch = detail.get("headRefName") or ""
        files = [f["path"] for f in detail.get("files") or [] if f.get("path")]
        if not self.root or not branch or not files:
            return False
        try:
            git(["fetch", "--quiet", "origin", "main", branch], self.root)
            return git(["diff", "--name-only", "origin/main", f"origin/{branch}", "--", *files], self.root) == ""
        except RuntimeError:
            return False

    def close_if_superseded(self, pr: dict, detail: dict, issue_no: int | None, review: str | None) -> list[str]:
        """An approved PR whose content main already has would go back to its builder for a rebase that leaves it
        empty (commonroom#42). Close it as superseded instead, and its issue as met. Returns [] if it isn't."""
        if review != "APPROVE" or not self.own_diff_empty(detail):
            return []
        self.board.close_pr(pr["number"], "Superseded: everything this PR changes is already on main, so there is "
                                          "nothing left to merge. Closed unmerged; the branch is kept.")
        if issue_no:
            self.board.close_issue(issue_no, f"Met: the content of its approved PR #{pr['number']} is already on main "
                                             f"(another PR carried it). PR #{pr['number']} was closed as superseded.")
        return [f"PR #{pr['number']} closed as superseded: its content is already on main"
                + (f"; issue #{issue_no} closed as met" if issue_no else "")]

    def already_updated(self, number: int, head: str) -> bool:
        """Did an earlier pass already update PR `number` while its head was `head`?"""
        return bool(head) and any(
            r["role"] == "update" and any(o.get("ref") == f"PR #{number}" and o.get("head") == head for o in r["outputs"])
            for r in self.ledger.recent_runs(self.project, limit=500))

    def record_update(self, issue_no: int | None, number: int, head: str):
        """A branch update is a ledger row like a merge: provider none, so no gate counts it as model time."""
        run = self.ledger.start_run(self.project, f"#{issue_no}" if issue_no else f"PR #{number}", "update", "none")
        self.ledger.finish_run(run, "done", outputs=[{"kind": "update", "ref": f"PR #{number}", "head": head}])

    def back_to_merge_main(self, pr: dict, issue_no: int, everything: list[dict], review: str | None) -> list[str]:
        """A PR that conflicts with main goes back to its builder. Unless review asked for changes, that is a merge-main
        run: Claude merges origin/main and resolves only the conflicts, so an approval stands (commonroom#14, #19)."""
        issue = next((i for i in everything if i["number"] == issue_no), None)
        if not issue or issue["state"] != "OPEN" or "status:ready" in labels(issue):
            return []
        if EXTERNAL in labels(issue):
            return self.tell_external(issue, f"PR #{pr['number']} {CONFLICTS_MAIN}. Its owner merges origin/main into the branch "
                                             f"and resolves the conflicts; the room doesn't build {EXTERNAL} issues.")
        self.board.set_labels(issue_no, add=["status:ready", "needs:changes"], remove=["status:review", "status:in-progress"])
        if review == "CHANGES":
            self.board.comment(issue_no, f"PR #{pr['number']} {CONFLICTS_MAIN}. The next Claude builder run merges "
                                         f"origin/main into the branch, resolves the conflicts and reruns the tests.")
            return [f"PR #{pr['number']} has merge conflicts; issue #{issue_no} back to its builder"]
        self.board.comment(issue_no, f"PR #{pr['number']} {CONFLICTS_MAIN}. A {MERGE_MAIN} merges origin/main into the "
                                     f"branch, resolves only the conflicts and reruns the tests. Nothing else changes, so "
                                     f"{'its approval stands' if review == 'APPROVE' else 'the review still sees only the PR'}.")
        return [f"PR #{pr['number']} has merge conflicts; issue #{issue_no} back to its builder for a {MERGE_MAIN}"]

    def tell_external(self, issue: dict, note: str) -> list[str]:
        """Say what the room would have sent back to its builder, without relabelling an issue someone outside the room
        owns. Once: not again while it is the issue's latest comment (housekeeping runs every pass)."""
        last = (issue.get("comments") or [{}])[-1].get("body") or ""
        if note not in last:
            self.board.comment(issue["number"], f"{MARK} {note}")
        return [f"#{issue['number']} is {EXTERNAL}: told its owner, didn't relabel ({note.split('.')[0]})"]

    def main_red(self, everything: list[dict], actions: list[str]) -> bool:
        """Has main's own CI failed (not merely been cancelled)? Then merges hold until it is green again, and a
        top-priority task to fix it exists (commonroom#19)."""
        try:
            runs = self.board.main_checks()
        except Exception as exc:
            actions.append(f"couldn't read main's CI, merging as usual: {str(exc)[:100]}")
            return False
        rollup = {"statusCheckRollup": runs}
        state = checks_state(rollup)
        rerunning = sorted({c.get("name") or "check" for c in runs if (c.get("attempt") or 1) > 1
                            and (c.get("conclusion") or "").upper() in ("IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED")})
        if state == "pending" and rerunning:
            actions.append(f"merges held: main's CI is being rerun ({', '.join(rerunning)})")
            return True
        if state != "red" or ci_infra_only(rollup):
            return False
        names = ", ".join(sorted({c.get("name") or "check" for c in failed_checks(rollup)}))
        # A run with a failed job and others still queued can't be rerun yet, and isn't a finished attempt either:
        # wait for it to complete (commonroom#98: every pass got "This workflow is already running").
        running = sorted({c["runId"] for c in failed_checks(rollup) if c.get("runId") and c.get("status")
                          and str(c["status"]).lower() != "completed"})
        if running:
            actions.append(f"merges held: main's CI is red ({names}); waiting for run {', '.join(running)} "
                           f"to finish before rerunning")
            return True
        # A first-attempt failure may be a flaky crash: rerun its failed jobs once before anyone is sent to fix it
        # (commonroom#88: one `gh run rerun --failed` passed where a whole builder run couldn't reproduce the crash).
        # A rerun GitHub refuses falls through to the fix task, so merges never hold silently (commonroom#98).
        first = sorted({c["runId"] for c in failed_checks(rollup) if c.get("runId") and c.get("attempt") == 1})
        rerun_error = ""
        if first:
            try:
                for run_id in first:
                    self.board.rerun_failed(run_id)
                actions.append(f"main's CI is red ({names}): merges held, rerunning once before opening a fix task")
                return True
            except Exception as exc:
                rerun_error = str(exc)[:100]
                actions.append(f"main's CI is red ({names}): the rerun failed: {rerun_error}")
        fix = next((i for i in everything if i["state"] == "OPEN" and i["title"].startswith(MAIN_RED)), None)
        if fix:
            actions.append(f"merges held: main's CI is red ({names}); #{fix['number']} fixes it")
            return True
        sha = next((c["sha"] for c in runs if c.get("sha")), "")[:7]
        links = "\n".join(f"- {c.get('name')}: {attempt_links(c)}" for c in failed_checks(rollup))
        twice = any((c.get("attempt") or 1) > 1 for c in failed_checks(rollup))
        test = (self.cfg.get("checks") or {}).get("test")
        why = (f", and the room's rerun of it failed: {rerun_error}" if rerun_error else
               ", twice: the room reran it once and it failed again" if twice else "")
        body = (f"Main's CI fails on {sha or 'its latest commit'}{why}:"
                f"\n{links}\n\nThe room holds every other merge until main is "
                f"green again; this task's PR is the one exception. Find the cause (often two PRs that each passed alone), "
                f"fix it on this issue's branch{f', run `{test}`' if test else ''}, and open a PR. Change nothing else.")
        try:
            number = self.board.create_issue(f"{MAIN_RED}: {names} failing on {sha or 'main'}", body,
                                             labels=["prio:top", "role:builder", "status:ready", "kind:bug"],
                                             milestone=self.milestone)
            actions.append(f"main's CI is red ({names}): merges held, opened #{number} to fix it")
        except Exception as exc:
            actions.append(f"main's CI is red ({names}): merges held; opening the fix task failed: {str(exc)[:100]}")
        return True

    def deploy(self) -> list[str]:
        """After merges: run the project's `[deploy] after_merge` command in the main checkout (e.g. re-export the web build)."""
        command = (self.cfg.get("deploy") or {}).get("after_merge")
        if not command or not self.root:
            return []
        # A failed deploy is reported, never fatal: the merge already happened and the loop must keep going.
        try:
            bash = find_bash()
            if not bash:
                return ["deploy FAILED: Git bash not found"]
            log_dir = Path(self.ledger.path).parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"deploy-{time.strftime('%Y%m%d-%H%M%S')}.log"
            # Godot's first import of freshly pulled files can fail transiently; one retry clears it.
            for attempt in (1, 2):
                done = subprocess.run([bash, "-lc", command], cwd=self.root, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=900)
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"--- attempt {attempt}, exit {done.returncode}\n{done.stdout}\n{done.stderr}\n")
                if done.returncode == 0:
                    restarted = services.restart_after_deploy(self.cfg, log_dir)
                    return [f"deploy ok{' (after a retry)' if attempt == 2 else ''}" + (f"; {', '.join(restarted)}" if restarted else "")]
            tail = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
            return [f"deploy FAILED twice: {tail[0][:120]} (full log: {log_path})"]
        except Exception as exc:
            return [f"deploy FAILED: {type(exc).__name__}: {str(exc)[:160]}"]

    def is_room_repo(self) -> bool:
        """Is this project the Common Room checkout this code is running from?"""
        if not self.root:
            return False
        return os.path.normcase(str(self.root.resolve())) == os.path.normcase(str(Path(self.room_root).resolve()))

    def update_live_checkout(self) -> list[str]:
        """After merging the room's own PRs, fast-forward the checkout the room runs from (`git pull --ff-only`; never
        a reset or a force). Safe while room processes run: python processes keep the modules they loaded at start
        (dispatch imports every room module up front), workers never run from this checkout, and heartbeat.cmd is one
        block that cmd.exe reads whole before running. New code takes effect as each process restarts."""
        if not self.is_room_repo():
            return []
        try:
            branch = git(["rev-parse", "--abbrev-ref", "HEAD"], self.root)
            if branch != "main":
                return [f"live checkout not updated: it is on {branch}, not main"]
            before = git(["rev-parse", "--short", "HEAD"], self.root)
            git(["pull", "--ff-only", "--quiet"], self.root)
            after = git(["rev-parse", "--short", "HEAD"], self.root)
            return [f"live checkout fast-forwarded {before}..{after}; room processes pick it up when they restart"]
        except Exception as exc:
            return [f"live checkout update FAILED, left as it was: {str(exc)[:160]}"]

    def playtest_after_merge(self, pr: dict, detail: dict, everything: list[dict]) -> list[str]:
        """A merged PR that changes a file matching `[playtester] paths` gets the game played (commonroom#53): it joins
        the open playtest issue, or opens one unless the last closed within `every_hours`. No table: nothing happens."""
        spec = self.cfg.get("playtester") or {}
        globs = list(spec.get("paths") or [])
        if not any(f.get("path") and any(fnmatch.fnmatchcase(f["path"], g) for g in globs) for f in detail.get("files") or []):
            return []
        number = pr["number"]
        name = pr.get("title") or detail.get("headRefName")
        line = f"- PR #{number}: {name}" if name else f"- PR #{number}"
        playtests = [i for i in everything if (i.get("title") or "").startswith(PLAYTEST_TITLE)
                     and "role:playtester" in labels(i)]
        try:
            current = next((i for i in playtests if i["state"] == "OPEN"), None)
            if current:
                listed = [int(n) for n in PLAYTEST_PR.findall(current.get("body") or "")]
                if number in listed:
                    return []
                body = (current.get("body") or "").rstrip("\n") + "\n" + line + "\n"
                title = f"{PLAYTEST_TITLE} " + ", ".join(f"#{n}" for n in listed + [number])
                self.board.edit_issue(current["number"], title, body)
                current["body"], current["title"] = body, title
                return [f"PR #{number} touches playtest paths: added to playtest #{current['number']}"]
            every = float(spec.get("every_hours", 6))
            closed = max((_stamp(i.get("closedAt")) for i in playtests), default=0.0)
            if closed and time.time() - closed < every * 3600:
                return [f"PR #{number} touches playtest paths; no playtest opened: the last closed "
                        f"{(time.time() - closed) / 3600:.1f} h ago, under [playtester] every_hours ({every:g})"]
            title = f"{PLAYTEST_TITLE} #{number}"
            body = (f"{MARK} Merged work touches the paths in `[playtester] paths`. Play the game as it is on main and "
                    f"report what looks or feels wrong.\n\nMerged PRs:\n{line}\n")
            created = self.board.create_issue(title, body, labels=("role:playtester", "status:ready"),
                                              milestone=self.milestone)
            everything.append({"number": created, "title": title, "body": body, "state": "OPEN", "comments": [],
                               "labels": [{"name": "role:playtester"}, {"name": "status:ready"}],
                               "milestone": {"title": self.milestone} if self.milestone else None})
            return [f"PR #{number} touches playtest paths: opened playtest #{created}"]
        except Exception as exc:   # the merge already happened; a failed board call is reported, never fatal
            return [f"PR #{number} touches playtest paths; updating the playtest issue failed: {str(exc)[:100]}"]

    def director_files(self, detail: dict) -> list[str]:
        """The PR's changed files that match `[merge] director_paths`."""
        return [f["path"] for f in detail.get("files") or [] if f.get("path")
                and any(fnmatch.fnmatchcase(f["path"], glob) for glob in self.director_paths)]

    def needs_director(self, detail: dict) -> bool:
        """The PR touches director paths, or is labelled `needs:director` because it needs lines in another project's
        room.toml or prompts, which the director lands there before approving (commonroom#65)."""
        return bool(self.director_files(detail)) or DIRECTOR_LABEL in labels(detail)

    def reviewer_view(self, detail: dict) -> dict:
        """On a PR that needs the director, the usual approval is the reviewer's: the director's own verdict is a
        second gate, so it neither stands in for the reviewer's nor hides it as the newest REVIEW comment."""
        if not self.needs_director(detail):
            return detail
        return detail | {"comments": [c for c in detail.get("comments") or []
                                      if not (c.get("body") or "").strip().startswith(DIRECTOR_MARK)]}

    def waits_for_director(self, detail: dict) -> bool:
        return self.needs_director(detail) and not director_approved(detail) and not self.carried_stamp(detail)

    def carried_stamp(self, detail: dict) -> str:
        """The director's stamp sha A that still counts for the PR head B, or ''. It does when every commit in A..B is
        a merge and no file changed in A..B matches `[merge] director_paths`, even one main changed: the director
        looks again whenever a director path moves (commonroom#168). Any doubt, or an error, means no."""
        head = head_sha(detail).lower()
        if not head:
            return ""
        for stamp in director_stamps(detail):
            if head.startswith(stamp):
                return ""
            key = (stamp, head)
            if key not in self.carry_cache:
                try:
                    found = self.board.compare_since(stamp, head)
                    self.carry_cache[key] = (merges_back_to(found["commits"], head, stamp)
                                             and not any(fnmatch.fnmatchcase(f, g) for f in found["files"] for g in self.director_paths))
                except Exception:
                    continue   # not cached: the next pass asks again
            if self.carry_cache[key]:
                return stamp
        return ""

    def director_rejected(self, detail: dict) -> bool:
        return self.needs_director(detail) and director_verdict(detail) == "CHANGES"

    def director_wait(self, number: int, detail: dict) -> str:
        files = self.director_files(detail)
        why = f"touches director paths ({', '.join(files)})" if files else f"is labelled {DIRECTOR_LABEL}"
        return (f"PR #{number} {why} and waits for the director's approval: post `{DIRECTOR_MARK} REVIEW: APPROVE` "
                f"with the line `HEAD: {head_sha(detail)}`")

    def ready_for_director(self) -> list[str]:
        """PRs with their usual approval, up to date with main, that wait only for the director's (commonroom#49). One
        the director already sent back, or one housekeeping still has to update, isn't theirs yet."""
        waiting = []
        for pr in self.board.open_prs():
            if not BRANCH.match(pr["headRefName"]):
                continue
            if not self.director_paths and DIRECTOR_LABEL not in labels(pr):
                continue   # nothing can make it wait: skip fetching its detail
            detail = self.board.pr_detail(pr["number"])
            if not self.waits_for_director(detail) or self.director_rejected(detail):
                continue
            view = self.reviewer_view(detail)
            if self.review_verdict(view) != "APPROVE" or not (checks_state(detail) == "green" if self.ci else self.tested(detail)):
                continue
            try:
                if self.board.behind_main(detail):
                    continue
            except Exception:
                continue   # housekeeping reports a PR it can't compare with main
            waiting.append(self.director_wait(pr["number"], detail))
        return waiting

    def tested(self, detail: dict) -> bool:
        """No CI: the approving review states a passing run of the project's test command on the PR head."""
        c = review_comment(self.reviewer_view(detail))
        return bool(c) and tested_on_head(c.get("body") or "", head_sha(detail), (self.cfg.get("checks") or {}).get("test"))

    def needs_review(self, detail: dict) -> bool:
        if not self.ci:
            # No CI: only an approval stating a passing test run on this exact head lets it merge, so an untested
            # approval, or one from before the room merged main into the branch, is reviewed again.
            view = self.reviewer_view(detail)
            return (latest_review(view) != "CHANGES" and not self.tested(detail)
                    and incomplete_reviews(view) < INCOMPLETE_ROUNDS)
        view = self.reviewer_view(detail)
        verdict = self.review_verdict(view)
        if verdict == "INCOMPLETE":
            return incomplete_reviews(view) < INCOMPLETE_ROUNDS and checks_state(detail) != "red"
        return verdict is None and checks_state(detail) != "red"

    def handle_red_ci(self, pr: dict, detail: dict, issue_no: int, everything: list[dict]) -> list[str]:
        failed = failed_checks(detail)
        if ci_infra_only(detail):
            runs = {m.group(1) for c in failed if (m := re.search(r"/actions/runs/(\d+)", c.get("detailsUrl") or ""))}
            done = []
            for run_id in sorted(runs):
                try:
                    ended = max((c.get("completedAt") or "" for c in failed), default="")
                    try:
                        since = (time.time() - calendar.timegm(time.strptime(ended[:19], "%Y-%m-%dT%H:%M:%S"))) / 60
                    except ValueError:
                        since = 1e9
                    attempt = self.board.rerun_ci(run_id, since)
                except Exception as exc:
                    done.append(f"PR #{pr['number']}: CI rerun failed: {str(exc)[:100]}")
                    continue
                if attempt:
                    done.append(f"PR #{pr['number']}: CI was cancelled/timed out (not a test failure); rerun attempt {attempt + 1}")
            return done
        issue = next((i for i in everything if i["number"] == issue_no), None)
        # While main itself is red, a PR fails for main's reason: only the fix task's own PR goes back (commonroom#43).
        fix = next((i for i in everything if i["state"] == "OPEN" and i["title"].startswith(MAIN_RED)), None)
        if fix and fix["number"] != issue_no:
            return [f"PR #{pr['number']}: CI red while main is red; left alone until #{fix['number']} lands"]
        # Only once the builder has finished (status:review); a builder still pushing will retrigger CI itself.
        if not issue or issue["state"] != "OPEN" or "status:review" not in labels(issue):
            return []
        names = ", ".join(sorted({c.get("name") or c.get("context") or "check" for c in failed}))
        # Failed before the last red main was fixed: bring main's fix into the branch so CI reruns on it, once. The
        # new run ends after the fix, so a failure on it is the PR's own and goes back to the builder.
        ended = max((c.get("completedAt") or "" for c in failed), default="")[:19]
        fixed = max((i.get("closedAt") or "" for i in everything
                     if i["state"] != "OPEN" and i["title"].startswith(MAIN_RED)), default="")[:19]
        if ended and ended < fixed:
            try:
                if self.board.behind_main(detail):
                    self.board.update_branch(pr["number"])
                    return [f"PR #{pr['number']}: CI failed while main was red; merged main into it so CI reruns"]
            except Exception as exc:
                if "conflict" not in str(exc).lower():
                    return [f"PR #{pr['number']}: updating it with main after main's fix failed: {str(exc)[:100]}"]
        if EXTERNAL in labels(issue):
            return self.tell_external(issue, f"CI is red on PR #{pr['number']}: {names}. Its owner fixes it on the same branch; "
                                             f"the room doesn't build {EXTERNAL} issues.")
        bounces = sum(1 for c in issue.get("comments") or [] if "CI is red" in (c.get("body") or ""))
        if bounces >= CI_BOUNCES:
            self.board.set_labels(issue_no, add=["status:blocked", "needs:director"], remove=["status:review"])
            self.board.comment(issue_no, f"CI is red again on PR #{pr['number']} ({names}) after {bounces} fixes. Escalating to the director.")
            return [f"PR #{pr['number']} still red after {bounces} fixes; #{issue_no} escalated"]
        self.board.set_labels(issue_no, add=["status:ready", "needs:changes"], remove=["status:review", "status:in-progress"])
        test = (self.cfg.get("checks") or {}).get("test") or "the project's tests"
        self.board.comment(issue_no, f"CI is red on PR #{pr['number']}: {names}. Read the failure with "
                                     f"`gh pr checks {pr['number']}` and `gh run view <run id> --log-failed`, fix it on the same "
                                     f"branch, and run `{test}` before pushing.")
        return [f"PR #{pr['number']} CI red ({names}); #{issue_no} back to its builder"]

    # Choosing work ---------------------------------------------------------------------------------
    def choose(self, spec: list[str], allowed: dict[str, gate.Verdict], exclude: str | None = None):
        choice = choose_provider(spec, allowed, exclude=exclude) or (choose_provider(spec, allowed) if exclude else None)
        if choice and self.only_provider and choice[0] != self.only_provider:
            return None
        return choice

    def busy_tasks(self) -> set[str]:
        """Tasks not to start now: running, or resting after room-errors. A room-error used to be retried on the next
        pass, so one leftover folder became five identical failures in two minutes (commonroom#9)."""
        now = time.time()
        resting = {task for task, (count, ended) in self.ledger.room_error_streaks(self.project, now).items()
                   if now - ended < ROOM_ERROR_BACKOFF * 2 ** min(count - 1, 4)}
        return self.ledger.running_tasks(self.project) | resting

    def ready_builds(self, open_now: list[dict]) -> list[dict]:
        running = self.busy_tasks()
        # A golden issue sent back to merge main still has an open PR, so it holds the others too.
        golden_held = {i["number"] for i in open_now if GOLDEN in labels(i)
                       and (labels(i) & {"status:in-progress", "status:review"} or f"#{i['number']}" in running
                            or merge_main_only(i))}
        ready = [i for i in open_now if {"status:ready", "role:builder"} <= labels(i) and not labels(i) & {"owner:human", EXTERNAL}
                 and f"#{i['number']}" not in running
                 and not (GOLDEN in labels(i) and golden_held - {i["number"]} and not merge_main_only(i))
                 and (not self.milestone or (i.get("milestone") or {}).get("title") == self.milestone
                      or any(l.startswith("provider:") for l in labels(i)))]
        # With none held, only the top golden issue is offered, so the Codex path's second pick can't be another one.
        fresh = [i for i in ready if GOLDEN in labels(i) and not merge_main_only(i)]
        if len(fresh) > 1:
            top = min(fresh, key=priority)
            ready = [i for i in ready if i not in fresh or i is top]
        return ready

    def ready_on_demand(self, open_now: list[dict]) -> list[tuple[dict, str]]:
        """Ready `role:<name>` issues in the goal milestone whose role is configured and has an agent file, each with
        that role. An issue for a role that isn't configured is skipped and holds nothing else up."""
        roles = runnable_roles(self.cfg) - BUILT_IN_ROLES
        running = self.busy_tasks()
        found = []
        for i in open_now:
            names = labels(i)
            role = next((l.split(":", 1)[1] for l in sorted(names) if l.startswith("role:") and l.split(":", 1)[1] in roles), None)
            if (role and "status:ready" in names and not names & {"owner:human", EXTERNAL} and f"#{i['number']}" not in running
                    and (not self.milestone or (i.get("milestone") or {}).get("title") == self.milestone)):
                found.append((i, role))
        return found

    def build_spec(self, issue: dict) -> list[str]:
        builder_spec = self.roles.get("builder", ["claude:sonnet"])
        # Merging main needs `git fetch`, which Codex's sandbox can't do, and little judgement: the cheap Claude model
        # takes it, whatever the task's own pin or size (commonroom#14, #19).
        if merge_main_only(issue):
            return self.roles.get("merge_main", ["claude:sonnet"])
        # `provider:codex` / `provider:claude` pins a task to the subscription that has the needed capability.
        pinned = [l.split(":", 1)[1] for l in labels(issue) if l.startswith("provider:")]
        # Large, high-risk and integration work goes to the heavy builder (Opus); routine work to the standard one.
        heavy = labels(issue) & {"size:L", "risk:high", "builder:heavy"}
        default = self.roles.get("builder_heavy", builder_spec) if heavy else builder_spec
        if conflicts_main(issue):
            # Review asked for changes and the PR conflicts: the run merges main too, so Claude takes it even over a
            # `provider:codex` pin (commonroom#51).
            claude = [e for e in [*pinned, *default] if e.partition(":")[0] == "claude"]
            return claude or ["claude:sonnet"]
        if pinned or heavy:
            return pinned or default
        # `[roles] builder_by_label` sends a kind of work to a provider first (commonroom#118): the first of the
        # issue's labels (sorted) found in the table picks the spec.
        by_label = self.roles.get("builder_by_label") or {}
        routed = next((by_label[l] for l in sorted(labels(issue)) if l in by_label), None)
        return routed or default

    def pace_headroom_ok(self, provider: str, v: gate.Verdict) -> bool:
        """Weekly use sits at least `[parallel] min_pace_headroom` under the pace line. An unpaced provider
        ([budget.<p>] pace = false) always passes."""
        pace, used = v.detail.get("weekly_pace"), v.detail.get("weekly_used")
        paced = ((self.cfg.get("budget") or {}).get(provider) or {}).get("pace", True)
        if not paced:
            return True
        return not (pace is None or used is None or pace - used < (self.cfg.get("parallel") or {}).get("min_pace_headroom", 15))

    def pick_background(self, open_now: list[dict], allowed: dict[str, gate.Verdict]) -> tuple[Job, str, str | None] | None:
        """A ready issue of a `[roles] background` role, only in spare allowance (commonroom#157): the provider's verdict
        allows it, pace headroom is there, the owner is away, the time is inside `background_window`, and no other
        autonomous run on that provider is running in any project, so the run's weekly delta is its own."""
        background = set(self.roles.get("background") or [])
        if not background or not in_window(self.roles.get("background_window"), time.time()):
            return None
        queue = [(i, r) for i, r in self.ready_on_demand(open_now) if r in background]
        for issue, role in sorted(queue, key=lambda pair: priority(pair[0])):
            choice = self.choose(self.roles[role], allowed)
            if not choice:
                continue
            provider = choice[0]
            v = allowed[provider]
            if v.detail.get("owner_active") or not self.pace_headroom_ok(provider, v):
                continue
            now = time.time()
            if any(r["status"] == "running" and not orphaned(r, now)
                   for r in self.ledger.recent_runs(None, limit=200, autonomous=True, provider=provider)):
                continue   # an orphan of a project nobody dispatches is not counted, and not closed here
            return Job("role", role, issue=issue, reason=f"#{issue['number']} is ready for the {role} (background, spare allowance)"), *choice
        return None

    def pick_extra(self, allowed: dict[str, gate.Verdict]) -> tuple[Job, str, str | None] | None:
        """An extra slot for the same provider, only while another is busy, weekly use sits well under the pace line
        and the 5-hour window is under `[parallel] max_5h` (commonroom#15). It takes reviews (short, and they unblock merges), then builds once the queue is deep, then research.
        Director, audits and housekeeping stay with slot 1."""
        par = self.cfg.get("parallel") or {}
        provider = self.only_provider or "claude"
        v = allowed.get(provider)
        if self.slot > int(par.get(f"{provider}_slots", 1)) or not v or not v.allowed:
            return None
        if not self.pace_headroom_ok(provider, v):
            return None
        if v.detail.get("owner_active"):
            return None   # extra slots also yield while the owner is using Claude elsewhere
        if (v.detail.get("five_hour_used") or 0) >= par.get("max_5h", 60):
            return None   # near the 5-hour reserve, finish what runs rather than start more (running work isn't stopped)
        busy = any(r["provider"] == provider and r["status"] == "running" for r in self.ledger.recent_runs(self.project, limit=20))
        if not busy:
            return None
        review = self.pick_review(allowed)   # full verdicts: a review meant for the other provider stays its own
        if review:
            return review
        open_now = self.board.open_issues(None)
        ready = sorted(self.ready_builds(open_now), key=priority)
        if len(ready) >= par.get("min_ready", 3):
            for issue in ready:
                choice = self.choose(self.build_spec(issue), {provider: v})
                if choice:
                    return Job("build", "builder", issue=issue, reason=f"#{issue['number']} is ready (parallel slot {self.slot})"), *choice
        running = self.busy_tasks()
        scouting = [i for i in open_now if {"status:ready", "role:scout"} <= labels(i) and f"#{i['number']}" not in running]
        for issue in sorted(scouting, key=priority):
            choice = self.choose(self.roles.get("scout", ["claude:sonnet"]), {provider: v})
            if choice:
                return Job("build", "scout", issue=issue, reason=f"#{issue['number']} research is ready (parallel slot {self.slot})"), *choice
        return None

    def pick_review(self, allowed: dict[str, gate.Verdict]) -> tuple[Job, str, str | None] | None:
        builder_spec = self.roles.get("builder", ["claude:sonnet"])
        issue_labels = None
        running = self.busy_tasks()
        for pr in self.board.open_prs():
            m = BRANCH.match(pr["headRefName"])
            # Without CI nothing merges unreviewed, so `review:skip` only applies where CI checks the PR.
            if not m or ("review:skip" in labels(pr) and self.ci) or f"PR #{pr['number']}" in running:
                continue
            if f"#{m.group(1)}" in running:
                continue   # its builder is still pushing: review the finished branch, not a draft (commonroom#124)
            detail = self.board.pr_detail(pr["number"])
            if self.needs_review(detail):
                if issue_labels is None:
                    issue_labels = {i["number"]: labels(i) for i in self.board.all_issues()}
                # Codex (the smaller subscription) is spent where independence matters most: high-risk changes.
                high_risk = "risk:high" in issue_labels.get(int(m.group(1)), set())
                spec = self.roles.get("reviewer_high_risk" if high_risk else "reviewer", builder_spec)
                built_by = self.last_provider(f"#{m.group(1)}", "builder")
                choice = self.choose(spec, allowed, exclude=built_by)
                if choice:
                    return Job("review", "reviewer", pr=detail, reason=f"PR #{pr['number']} awaits {'high-risk ' if high_risk else ''}review"), *choice
        return None

    def pick(self, allowed: dict[str, gate.Verdict]) -> tuple[Job, str, str | None] | None:
        if self.slot > 1:
            return self.pick_extra(allowed)
        review = self.pick_review(allowed)
        if review:
            return review
        builder_spec = self.roles.get("builder", ["claude:sonnet"])
        open_now = self.board.open_issues(None)
        ready = self.ready_builds(open_now)
        running = self.busy_tasks()
        scouting = [i for i in open_now if {"status:ready", "role:scout"} <= labels(i) and f"#{i['number']}" not in running]
        background = set(self.roles.get("background") or [])
        # The owner waiting on an answer outranks builds: a reply should never sit behind the whole queue.
        owner = self.owner_trigger()
        if owner:
            choice = self.choose(self.roles.get("director", ["claude:opus"]), allowed)
            if choice:
                return Job("direct", "director", reason=owner), *choice
        # An escalation stops work that only the director can unblock, so it outranks the build queue too
        # (commonroom#29: behind 26 ready builds it never ran). DIRECTOR_COOLDOWN still caps it at one run an hour.
        trigger = self.director_trigger()
        if trigger:
            choice = self.choose(self.roles.get("director", ["claude:opus"]), allowed)
            if choice:
                return Job("direct", "director", reason=trigger), *choice
        # The periodic audit: the frontier model steps back, judges everything, acts, and reports to the owner.
        audit = self.audit_due()
        if audit:
            choice = self.choose(self.roles.get("auditor", ["claude:fable"]), allowed)
            if choice:
                return Job("audit", "auditor", reason=audit), *choice
        scout_spec = self.roles.get("scout", ["claude:sonnet"])
        # Research must not starve behind an endless build queue: a ready scout task goes first every few hours.
        if scouting and time.time() - self.last_run_at("scout") > SCOUT_INTERLEAVE:
            choice = self.choose(scout_spec, allowed)
            if choice:
                issue = min(scouting, key=priority)
                return Job("build", "scout", issue=issue, reason=f"#{issue['number']} research is ready"), *choice
        pinned_here = [i for i in ready if f"provider:{self.only_provider}" in labels(i)
                       and not (self.only_provider == "codex" and conflicts_main(i))] if self.only_provider else []
        if self.only_provider == "codex" and self.roles.get("codex_parallel_builds", True) and not pinned_here:
            # Work only Codex can do (pinned, e.g. image generation) comes before routine parallel builds.
            # Codex shouldn't idle while there's a queue: it takes the second routine task in parallel,
            # leaving the top-priority one for Claude, and never the heavy (Opus) work.
            # Codex renders Godot off-screen fine in its sandbox (gamemirage#101); device-level perf work stays with Claude.
            routine = [i for i in sorted(ready, key=priority)
                       if not labels(i) & ({"size:L", "risk:high", "builder:heavy"} | CODEX_CANNOT)
                       and not any(l.startswith("provider:") for l in labels(i)) and not conflicts_main(i)]
            claude_busy = any(r["provider"] == "claude" and r["status"] == "running"
                              for r in self.ledger.recent_runs(self.project, limit=20))
            # Leave the top routine task for Claude, unless Claude is already busy on something else.
            take = routine[0] if (routine and claude_busy) else (routine[1] if len(routine) >= 2 else None)
            if take and allowed.get("codex") and allowed["codex"].allowed:
                return Job("build", "builder", issue=take, reason=f"#{take['number']} is ready (parallel Codex build)"), "codex", None
        # On-demand roles (playtester, analyst...) queue with the builds by priority, so they never starve behind them.
        queue = [(i, "builder") for i in ready] + [(i, r) for i, r in self.ready_on_demand(open_now) if r not in background]
        for issue, role in sorted(queue, key=lambda pair: priority(pair[0])):
            if role != "builder":
                choice = self.choose(self.roles[role], allowed)
                if choice:
                    return Job("role", role, issue=issue, reason=f"#{issue['number']} is ready for the {role}"), *choice
                continue
            choice = self.choose(self.build_spec(issue), allowed)
            # Codex's sandbox can't start a browser, so device-perf work never falls back to it (gamemirage#121),
            # unless the issue is explicitly pinned to Codex.
            if choice and choice[0] == "codex" and labels(issue) & CODEX_CANNOT and "provider:codex" not in labels(issue):
                continue
            if choice:
                return Job("build", "builder", issue=issue, reason=f"#{issue['number']} is ready"), *choice
        for issue in sorted(scouting, key=priority):
            choice = self.choose(scout_spec, allowed)
            if choice:
                return Job("build", "scout", issue=issue, reason=f"#{issue['number']} research is ready"), *choice
        sweep = self.capability_sweep_due()
        if sweep and self.choose(scout_spec, allowed):
            issue = self.create_sweep_issue(sweep)
            if issue:
                return Job("build", "scout", issue=issue, reason=f"scheduled capability sweep: {sweep}"), *self.choose(scout_spec, allowed)
        return self.pick_background(open_now, allowed)

    def last_run_at(self, role: str) -> float:
        return next((r["started_at"] for r in self.ledger.recent_runs(self.project, limit=200) if r["role"] == role), 0.0)

    def report_path(self) -> Path:
        """Where the auditor writes its report: the run's own working copy, as an absolute path. An auditor whose
        worktree vanished moved to a new one and still wrote here, and the report was picked up (commonroom#17)."""
        return Path(getattr(self, "_worktree", ".")).resolve() / ".room-report.md"

    def reports_dir(self) -> Path:
        return Path(self.ledger.path).parent / "reports" / (self.project or "default")

    def quiet_merge(self, detail: dict, issue_no: int | None) -> str | None:
        """Why a merge gives an audit nothing new to look at, or None (commonroom#103): every changed file matches
        `[audit] quiet_paths` ("docs"), or it is the PR of an auditor run: not an issue branch, opened while an
        auditor was running ("auditor"). Audit PR #538 re-captured the build #521 had captured, after #521 merged."""
        files = [f["path"] for f in detail.get("files") or [] if f.get("path")]
        globs = list((self.cfg.get("audit") or {}).get("quiet_paths") or [])
        if files and globs and all(any(fnmatch.fnmatchcase(path, g) for g in globs) for path in files):
            return "docs"
        created = detail.get("createdAt")
        if issue_no is None and created:
            try:
                at = calendar.timegm(time.strptime(str(created)[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                return None
            if any(r["role"] == "auditor" and r["started_at"] <= at <= (r["ended_at"] or time.time())
                   for r in self.ledger.recent_runs(self.project, limit=200)):
                return "auditor"
        return None

    def audit_due(self) -> str | None:
        """Every `[audit] every_hours` if anything happened since the last audit (or every 6h regardless). Merges of the
        auditor's own PRs and of `quiet_paths`-only PRs aren't something happening (commonroom#103)."""
        audit = self.cfg.get("audit") or {}
        if not audit or not audit.get("enabled", True):  # opt-in per project via [audit] in room.toml
            return None
        every = float(audit.get("every_hours", 1))
        last = self.last_run_at("auditor")
        age = time.time() - last
        # Fable has its own weekly window: keep a quarter of it for the owner.
        fable = self.ledger.latest_usage("claude").get("seven_day_fable")
        if fable and fable["used_pct"] > 75:
            return None
        activity = [r for r in self.ledger.recent_runs(self.project, limit=200)
                    if r["started_at"] > last and r["role"] not in ("auditor",)
                    and not (r["role"] == "merge" and any(o.get("quiet") for o in r["outputs"]))]
        if age < every * 3600:
            # Something going wrong brings the audit forward (at most every 30 min): a breaker, or 3+ wasted runs.
            wasted = [r for r in activity if r["status"] in ("failed", "blocked", "room-error")]
            breaker = bool(self.ledger.open_breakers(self.project))
            if age >= 1800 and (breaker or len(wasted) >= 3):
                return (f"early audit: {len(wasted)} runs didn't land since the last audit"
                        + (" and a breaker is open" if breaker else ""))
            return None
        if not activity and age < 6 * 3600:
            return None
        hours = round(min(age / 3600, 24), 1) if last else every
        return f"periodic audit of the last {hours}h ({len(activity)} runs since the previous audit)"

    def capability_sweep_due(self) -> str | None:
        """A recurring research sweep, rotating through focus areas, every `[scout] every_hours`."""
        scout = self.cfg.get("scout") or {}
        every, focus = scout.get("every_hours", 24), scout.get("focus") or []
        if not focus or time.time() - self.last_run_at("scout") < every * 3600:
            return None
        sweeps = sum(1 for r in self.ledger.recent_runs(self.project, limit=500) if r["role"] == "scout")
        return focus[sweeps % len(focus)]

    def create_sweep_issue(self, focus: str) -> dict | None:
        if self.dry_run:
            return {"number": 0, "title": f"Capability sweep: {focus}", "labels": []}
        today = time.strftime("%Y-%m-%d")
        # `[scout] environment`: where this machine's tools and limits are written down (commonroom#151).
        environment = (self.cfg.get("scout") or {}).get("environment")
        body = (f"Scheduled capability sweep ({today}). Focus: **{focus}**.\n\n"
                "Find tools, repos, MCP servers, techniques or references that are free (or already paid for), available on "
                f"this machine{f' ({environment})' if environment else ''}, and would serve the project's mission or make "
                "the studio faster, cheaper or more capable. "
                "Be ambitious, but separate the proven from the hyped.\n\n## Deliverable\n"
                f"- `docs/scouting/{today}-{slug(focus)}.md`: each finding with evidence links, what it would change, cost and risk, "
                "and a proposed experiment with a pass/fail test.\n"
                "- Up to 3 `kind:suggestion` issues for the best experiments. The director decides on them.")
        url = board_mod.gh(["issue", "create", "--repo", self.cfg["project"]["repo"], "--title", f"Capability sweep: {focus}",
                            "--body", body, "--label", "kind:research", "--label", "role:scout", "--label", "status:ready"])
        number = int(str(url).rstrip("/").rsplit("/", 1)[1])
        return {"number": number, "title": f"Capability sweep: {focus}", "labels": [{"name": "role:scout"}], "body": body}

    def owner_trigger(self) -> str | None:
        """New owner comments on owner tasks or suggestions, answered within minutes rather than the hourly cooldown."""
        last = next((r for r in self.ledger.recent_runs(self.project, limit=50) if r["role"] == "director"), None)
        since = last["started_at"] if last else 0
        if time.time() - since < OWNER_COOLDOWN:
            return None
        reasons = []
        # The board passes only trusted logins' issues and comments (commonroom#176), but agents post as the owner's
        # account, so authorship can't tell them apart. Agent comments start with `[agent:...]`; the owner-channel
        # bulletin starts with `@<owner>` instead so it reaches his phone.
        mention = "@" + self.owner
        for issue in self.board.open_issues(None):
            names = labels(issue)
            comments = issue.get("comments") or []
            # Only unanswered: the last word must be the owner's, and new since the last director run.
            last_body = (comments[-1].get("body") or "").lstrip() if comments else ""
            last_is_owner = bool(comments) and not last_body.startswith(("[", mention))
            # A brand-new owner suggestion (not one an agent filed) gets the fast lane too.
            fresh_suggestion = ("kind:suggestion" in names and "triaged" not in names and not comments
                                and not (issue.get("body") or "").lstrip().startswith("[agent:"))
            if fresh_suggestion:
                reasons.append(f"#{issue['number']} is a new owner suggestion: triage it (accept, reject with reasons, or park)")
            elif names & {"owner:human", "kind:suggestion"} and last_is_owner and _after(comments[-1], since):
                reasons.append(f"#{issue['number']} has a new owner comment: answer it, accept the delivery, or close the task")
        return "; ".join(reasons) or None

    def director_trigger(self) -> str | None:
        last = next((r for r in self.ledger.recent_runs(self.project, limit=50) if r["role"] == "director"), None)
        since = last["started_at"] if last else 0
        if time.time() - since < DIRECTOR_COOLDOWN:
            return None
        reasons = []
        # Escalations and suggestions count whatever their milestone: a suggestion the director hasn't placed
        # yet has none (commonroom#10). The rest of the checks are about the goal milestone only.
        every_open = self.board.open_issues(None)
        interactive = []
        for issue in every_open:
            names = labels(issue)
            if "needs:director" in names:
                # Only the owner's interactive session can do this one; a headless run could only re-read it (commonroom#134).
                if INTERACTIVE_PHRASE in (issue.get("body") or "").lower():
                    interactive.append(f"#{issue['number']} waits for the interactive session")
                else:
                    reasons.append(f"#{issue['number']} escalated to the director")
            if "kind:suggestion" in names and "triaged" not in names:
                reasons.append(f"#{issue['number']} is an untriaged owner suggestion")
        open_issues = [i for i in every_open
                       if self.milestone and (i.get("milestone") or {}).get("title") == self.milestone]
        for issue in open_issues:
            names = labels(issue)
            if "owner:human" in names and any(_after(c, since) for c in owner_comments(issue)):
                reasons.append(f"#{issue['number']} has a new owner comment (delivery or answer)")
        busy = [i for i in open_issues if labels(i) & {"status:ready", "status:in-progress", "status:review", EXTERNAL}]
        owner_open = {i["number"] for i in open_issues if "owner:human" in labels(i)}
        # Work that only waits on the owner is not a reason to wake the director.
        machine_open = [i for i in open_issues if "owner:human" not in labels(i)
                        and not (dependencies(i.get("body")) & owner_open)]
        if machine_open and not busy:
            reasons.append("the milestone has open work but nothing is ready: unblock, re-plan or split tasks")
        reasons += self.ready_for_director()
        bulletin = self.bulletin_missed(since)
        if bulletin:
            reasons.append(bulletin)
        # The director is always on it (owner, 9 Oct; commonroom#41): even with no event, it steers every few hours.
        steer = float((self.cfg.get("director") or {}).get("steer_every_hours", 3) or 0)
        if steer and time.time() - since >= steer * 3600:
            reasons.append(steer_reason(self.cfg))
            reasons += interactive
        return "; ".join(reasons) or None

    def bulletin_missed(self, last_director: float, now: float | None = None) -> str | None:
        """The owner's digest is due by each `[bulletin] deadlines` time (commonroom#82). Missed: the latest passed
        deadline has no `@<owner>` comment on `[bulletin] issue` since the deadline before it (bulletins go out early),
        and no director run started after it. Any such run was given this reason, so it fires once per deadline."""
        cfg = self.cfg.get("bulletin") or {}
        if not cfg.get("issue") or not cfg.get("deadlines"):
            return None
        previous, due, label = bulletin_window(cfg["deadlines"], time.time() if now is None else now)
        if last_director > due:
            return None
        owner = self.owner
        try:
            comments = self.board.issue_comments(int(cfg["issue"]), previous)
        except Exception as exc:
            self.log(f"   bulletin check failed: {exc}")
            return None
        if any(_after(c, previous) and (c.get("body") or "").lstrip().startswith("@" + owner) for c in comments):
            return None
        return BULLETIN_REASON.format(due=label, issue=cfg["issue"], owner=owner)

    def last_provider(self, task: str, role: str) -> str | None:
        for r in self.ledger.recent_runs(self.project, limit=200):
            if r["task"] == task and r["role"] == role:
                return r["provider"]
        return None

    # Running work ----------------------------------------------------------------------------------
    def run_job(self, job: Job, provider: str, model: str | None) -> dict:
        role = load_role(self.root, job.role)
        task = f"#{job.issue['number']}" if job.issue else (f"PR #{job.pr['number']}" if job.pr else None)
        self.log(f"-> {job.kind} {task or ''} with {provider}{':' + model if model else ''} ({job.reason})")
        if self.dry_run:
            return {"status": "dry-run"}
        # Claim first, atomically: parallel dispatchers must never work the same task twice.
        effort = (self.cfg.get("effort") or {}).get(job.role)
        run_id = self.ledger.start_run(self.project, task, job.role, provider,
                                       model or (role.model if provider == "claude" else "default"),
                                       effort=effort or "default", exclusive=bool(task))
        if run_id is None:
            self.log(f"   {task} is already being worked on; skipped")
            return {"status": "skipped", "outputs": [], "note": "already running"}
        self._run_id = run_id
        # The window at decision time, for audits (commonroom#15).
        self.ledger.db.execute("UPDATE runs SET five_hour_start=? WHERE id=?", (self.five_hour_used(provider), run_id))
        self.ledger.db.commit()
        try:
            git(["fetch", "--quiet", "origin"], self.root)
            worktree, branch = self.prepare_worktree(job, provider)
            self.ledger.set_worktree(run_id, worktree)   # removal guard, and no project mistakes this worker's sessions for the owner
        except Exception as exc:
            self.room_error(run_id, job, task, exc)
            raise
        labelled = False
        result = None
        try:
            before = self.remote_head(branch)
            weekly_start = self.weekly_used(provider)
            if job.issue:
                self.board.set_labels(job.issue["number"], add=["status:in-progress"], remove=["status:ready", "needs:changes"])
                labelled = True
            started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._worktree, self._provider = worktree, provider
            resume = self.resumable_run(job, provider, task, worktree, run_id)
            if resume:
                self.log(f"   resuming session {resume['session_id']} from run {resume['id']}")
                prompt = self.continuation_prompt(job, branch, resume) + self.headless_note()
            else:
                prompt = self.prompt(job, branch) + self.why() + self.headless_note()
            result = run_worker(provider, role, prompt, worktree, self.ledger, self.log_dir,
                                resume_session=resume["session_id"] if resume else None,
                                model=model, effort=effort, timeout=workers_mod.RUN_TIMEOUT, scratch_env=(self.cfg.get("codex") or {}).get("scratch_env"),
                                stop_check=lambda: self.stop_reason(provider))
            if result.limited_until or result.interrupted:
                status, outputs, note = self.requeue(job, provider, result)
            else:
                if provider == "codex" and job.kind == "build":
                    self.publish_sandboxed_work(job, worktree, branch, result)
                status, outputs, note = self.assess(job, branch, before, started_iso, result)
            if resume:
                note = f"resumed session {resume['session_id']} from run {resume['id']}; {note}"
            self.ledger.finish_run(run_id, status, outputs, result.input_tokens, result.output_tokens, result.cost_usd,
                                   note, session_id=result.session_id)
            self.ledger.db.execute("UPDATE runs SET weekly_start=?, weekly_end=? WHERE id=?",
                                   (weekly_start, self.weekly_used(provider), run_id))
            self.ledger.db.commit()
            if job.kind not in ("build", "role", "review") and status not in ("limited", "stopped"):
                self.verify_actions(run_id, result, started_iso)
        except Exception as exc:
            # A room-side error is not a model failure: put the task back and record the error honestly.
            if labelled:
                try:
                    self.board.set_labels(job.issue["number"], add=["status:ready"], remove=["status:in-progress"])
                except Exception:
                    pass
            if run_id:
                self.room_error(run_id, job, task, exc)
            raise
        finally:
            self.preserve_work(job, worktree, branch)
            if result is not None:
                self.report_timeout(job, branch, result)
            self.cleanup_worktree(worktree, run_id)
            shutil.rmtree(worker_tmp(worktree), ignore_errors=True)
        self.log(f"   {status}: {', '.join(o['ref'] for o in outputs) or 'no output'}  {note[:200]}")
        return {"status": status, "outputs": outputs, "note": note, "session": result.session_id}

    def resumable_run(self, job: Job, provider: str, task: str | None, worktree: Path, run_id: str) -> dict | None:
        """The earlier Claude run of this build or review to continue instead of starting a fresh session (commonroom#142),
        or None. It must have been stopped or limited, less than 12 h ago, in this same working folder, with its
        session file still on disk, and nothing may have run since; after two resumes in a row the third try starts fresh."""
        if provider != "claude" or job.kind not in ("build", "review") or not task:
            return None
        runs = self.ledger.task_runs(self.project, task, 3, exclude=run_id)
        last = runs[0] if runs else None
        if (not last or last["role"] != job.role or last["provider"] != "claude" or last["status"] not in ("stopped", "limited")
                or not last["session_id"] or not last["ended_at"] or time.time() - last["ended_at"] > RESUME_WINDOW
                or str(last.get("worktree") or "").lower() != str(Path(os.path.abspath(worktree))).lower()
                or not session_file(worktree, last["session_id"], self.claude_projects).exists()):
            return None
        if len(runs) == 3 and all(r["status"] in ("stopped", "limited") for r in runs):
            return None
        return last

    def continuation_prompt(self, job: Job, branch: str | None, earlier: dict) -> str:
        """What a resumed session is told: why it stopped, where its work is, and to look again before carrying on."""
        why = earlier.get("note") or earlier["status"]
        if job.kind == "review":
            sha = (job.pr or {}).get("headRefOid") or self.remote_head(branch) or "unknown"
            where = (f"PR #{job.pr['number']} is now at head {sha}. Check whether it moved since "
                     f"you started, and review what is there now.")
        else:
            where = (f"Your work so far is on branch `{branch}` (anything uncommitted was preserved as a WIP commit). "
                     f"Check the issue's comments and labels too before you continue.")
        return (f"Your earlier run on this task was stopped before it finished: {why}. You are resuming that session in the "
                f"same working folder. {where} Re-check the branch and the board for changes since you stopped, then continue "
                f"where you left off and finish the task.\n"
                f"Finish with a short report, and make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")

    def report_timeout(self, job: Job, branch: str | None, result):
        """A build or role run the room ended at its time limit says so on its issue, so the next attempt and the
        director can tell it ran out of time and whether anything survived (commonroom#117)."""
        if (job.kind not in ("build", "role") or not job.issue or self.dry_run
                or not any("timed out after" in e for e in result.errors)):
            return
        minutes = round(workers_mod.RUN_TIMEOUT / 60)
        try:
            if branch and self.pushed_commits(branch):
                kept = f"Its work was preserved on branch `{branch}`; the next attempt continues from it."
            else:
                kept = ("Nothing was committed, so nothing was preserved and the next attempt starts over. "
                        "If this keeps happening, the issue may need splitting.")
            self.board.comment(job.issue["number"],
                               f"The room ended this run at its {minutes}-minute time limit. {kept}")
        except Exception as exc:
            self.log(f"   could not comment on the timeout: {str(exc)[:160]}")

    def pushed_commits(self, branch: str) -> int:
        """Commits on the pushed branch that main doesn't have."""
        if not self.remote_head(branch):
            return 0
        return int(git(["rev-list", "--count", f"origin/main..origin/{branch}"], self.root) or 0)

    def verify_actions(self, run_id: str, result, started_iso: str):
        """Record the ACTIONS claims GitHub doesn't show (commonroom#68). Records only: the run's status, outputs and
        breakers stay as they were, and a failure here is logged, never raised."""
        fetch = getattr(self.board, "claim_record", None)
        actions = actions_of(result.final_text)
        if not fetch or not actions:
            return
        try:
            since = calendar.timegm(time.strptime(started_iso, "%Y-%m-%dT%H:%M:%SZ"))
            checked, unverified = check_claims(actions, since, fetch, self.ledger.projects() - {self.project})
            self.ledger.record_claims(run_id, len(checked), unverified)
            if unverified:
                self.log(f"   {len(unverified)} of {len(checked)} reported actions didn't apply: {'; '.join(unverified)[:300]}")
        except Exception as exc:
            self.log(f"   could not check the run's actions: {str(exc)[:160]}")

    def room_error(self, run_id: str, job: Job, task: str | None, exc: Exception):
        """Record a room-side error. The task then rests (`busy_tasks`) instead of being retried on the next pass, and
        the third in a row goes to the director with the error, the way repeated builder failures do (commonroom#9)."""
        note = f"{type(exc).__name__}: {exc}"[:800]
        self.ledger.finish_run(run_id, "room-error", [], note=note)
        streak = self.ledger.room_error_streaks(self.project).get(task, (0, 0))[0] if task else 0
        m = BRANCH.match((job.pr or {}).get("headRefName") or "")
        n = job.issue["number"] if job.issue else int(m.group(1)) if m else None
        if streak != ROOM_ERROR_ESCALATE or n is None or self.dry_run:
            return
        try:
            if job.issue:   # a build stops here; a review keeps backing off while the director looks
                self.board.set_labels(n, add=["status:blocked", "needs:director"], remove=["status:ready", "status:in-progress"])
            else:
                self.board.set_labels(n, add=["needs:director"])
            self.board.comment(n, f"{task} hit {streak} room errors in a row (failures in Common Room itself, not the "
                                  f"agent's), so the room stopped retrying it at once and is escalating to the director. "
                                  f"Last error:\n\n```\n{note}\n```")
        except Exception as err:
            self.log(f"   could not escalate {task}: {str(err)[:160]}")

    def why(self) -> str:
        """Workers optimise what they're told. Give them the goal the task serves, not only its acceptance criteria."""
        mission = (self.cfg.get("project") or {}).get("mission")
        parts = [f"the milestone **{self.milestone}**" if self.milestone else None, f"the mission: {mission.rstrip('.')}" if mission else None]
        parts = [p for p in parts if p]
        return ("\n\nWhy this matters: it serves " + " and ".join(parts) + ". Judge your work against that goal as well as "
                "the task's criteria.") if parts else ""

    def requeue(self, job: Job, provider: str, result) -> tuple[str, list, str]:
        """The provider refused (usage limit) or the room stopped the worker. Neither is the task's fault:
        put it back untouched, without counting a failed attempt, and rest the provider until its limit resets."""
        if result.limited_until:
            self.ledger.set_cooldown(provider, result.limited_until, "usage limit hit")
            status = "limited"
            note = f"{provider} usage limit hit; resting until {time.strftime('%H:%M', time.localtime(result.limited_until))}"
        else:
            status, note = "stopped", f"stopped by the room: {result.interrupted}"
        if job.issue:
            self.board.set_labels(job.issue["number"], add=["status:ready"], remove=["status:in-progress"])
        return status, [], note

    def preserve_work(self, job: Job, worktree: Path, branch: str | None):
        """Never throw work away: uncommitted changes on a build branch are committed as WIP and pushed
        before the working copy is deleted, so the next attempt continues from them."""
        if job.kind != "build" or not branch or not worktree.exists() or self.dry_run:
            return
        committed = False
        try:
            if git(["status", "--porcelain"], worktree):
                git(["add", "-A"], worktree)
                git(["commit", "-q", "-m", "WIP: unfinished work preserved by Common Room\n\nThe worker stopped before "
                     "finishing; the next attempt continues from this commit."], worktree)
                committed = True
            # Also catch commits that exist only here (e.g. the room committed Codex's work, then the push failed).
            try:
                base = git(["rev-parse", "--verify", "-q", f"origin/{branch}"], worktree)
            except RuntimeError:
                base = "origin/main"
            if int(git(["rev-list", "--count", f"{base}..HEAD"], worktree) or 0) == 0:
                return
            git(["push", "-q", "-u", "origin", branch], worktree)
            if committed and job.issue:
                self.board.comment(job.issue["number"], f"Unfinished work was preserved on branch `{branch}`. "
                                                        f"The next attempt continues from it.")
        except Exception as exc:
            self.log(f"   could not push work on {branch}: {str(exc)[:160]}")
            self.rescue(job, worktree, branch)

    def rescue(self, job: Job, worktree: Path, branch: str):
        """Last resort before the working copy is deleted: bundle its unpushed commits to a local file."""
        try:
            folder = Path(self.ledger.path).parent / "rescue"
            folder.mkdir(parents=True, exist_ok=True)
            bundle = folder / f"{branch}-{int(time.time())}.bundle"
            git(["bundle", "create", str(bundle), "HEAD", "^origin/main"], worktree)
            self.log(f"   rescued unpushed work to {bundle}")
            if job.issue:
                self.board.comment(job.issue["number"], f"Pushing this branch failed, so its unpushed work was saved "
                                                        f"locally as `{bundle}` (`git fetch <bundle> HEAD:{branch}` restores it).")
        except Exception as exc:
            self.log(f"   could not rescue work on {branch}: {str(exc)[:160]}")

    def publish_sandboxed_work(self, job: Job, worktree: Path, branch: str, result):
        """Codex's sandbox keeps .git read-only, so the room commits, pushes and opens the PR for its changes."""
        changes = git(["status", "--porcelain"], worktree)
        if not changes or re.search(r"STATUS:\s*BLOCKED", result.final_text or "", re.I):
            return
        text = result.final_text or ""
        title = (re.search(r"^PR_TITLE:\s*(.+)$", text, re.M) or [None, f"#{job.issue['number']}: {job.issue['title']}"])[1].strip()
        body = text.split("PR_BODY:", 1)[1].strip() if "PR_BODY:" in text else text.strip()
        body = re.sub(r"\n?STATUS:\s*(DONE|BLOCKED)\s*$", "", body).strip()
        git(["add", "-A"], worktree)
        git(["commit", "-q", "-m", f"{title}\n\nBuilt by Codex in a sandbox; committed and pushed by Common Room."], worktree)
        git(["push", "-q", "-u", "origin", branch], worktree)
        if not self.board.pr_for_branch(branch):
            board_mod.gh(["pr", "create", "--repo", self.cfg["project"]["repo"], "--head", branch, "--base", "main",
                          "--title", title[:200], "--body", f"Closes #{job.issue['number']}\n\n{body}\n\n_Built by Codex; published by Common Room._"])

    def codex_handoff(self, job: Job, branch: str | None, text: str) -> bool:
        """A Codex build that ended BLOCKED usually hit its sandbox (no `gh` auth, a gate it can't run), not the task.
        A Claude builder doesn't have those limits, so it gets the issue instead of the director: from the changes
        `preserve_work` pushes to the branch (commonroom#84), or from the issue when Codex stopped before editing
        anything (commonroom#91). Work pinned `provider:codex` still escalates, and so does any Claude build, so this
        can't loop."""
        if (self._provider != "codex" or job.role != "builder" or not branch
                or "provider:codex" in labels(job.issue)):
            return False
        worktree = getattr(self, "_worktree", None)
        try:
            changed = bool(worktree and git(["status", "--porcelain"], worktree))
        except (RuntimeError, OSError):
            changed = False
        n = job.issue["number"]
        if hasattr(self.board, "ensure_label"):
            self.board.ensure_label("provider:claude")
        self.board.set_labels(n, add=["status:ready", "provider:claude"], remove=["status:in-progress"])
        if changed:
            lead = (f"Codex finished part or all of this but ended BLOCKED. Its work is preserved on branch `{branch}`. "
                    f"A Claude builder continues from it: run the gate, finish what's left, open the PR.")
        else:
            lead = (f"Codex ended BLOCKED before changing anything, so this run left nothing on branch `{branch}`. "
                    f"A Claude builder starts from the issue: build it, run the gate, open the PR.")
        self.board.comment(n, f"{lead}\n\n```\n{text.strip()[-1500:]}\n```")
        return True

    def assess(self, job: Job, branch: str | None, before: str | None, started_iso: str, result) -> tuple[str, list, str]:
        tail = (result.final_text or "").strip()[-600:]
        blocked = bool(re.search(r"STATUS:\s*BLOCKED", result.final_text or "", re.I))
        if job.kind == "build":
            n = job.issue["number"]
            pr = self.board.pr_for_branch(branch)
            after = self.remote_head(branch)
            if pr and pr["state"] == "OPEN" and after and after != before:
                self.to_review(n)
                if job.role == "scout" and hasattr(self.board, "label_pr"):
                    self.board.label_pr(pr["number"], "review:skip")
                return "done", [{"kind": "pr", "ref": f"PR #{pr['number']}"}], tail
            if (pr and pr["state"] == "OPEN" and not blocked
                    and re.search(r"STATUS:\s*DONE", result.final_text or "", re.I)):
                # The builder checked and found nothing left to change (e.g. the fix already landed and was approved
                # while the issue sat in the queue). That's a finished task, not a failed attempt.
                self.to_review(n)
                return "done", [{"kind": "pr", "ref": f"PR #{pr['number']} (no changes needed)"}], tail
            if blocked and self.codex_handoff(job, branch, result.final_text or ""):
                return "blocked", [{"kind": "blocked-report", "ref": f"#{n} blocked, handed to Claude"}], tail
            if blocked:
                self.board.set_labels(n, add=["status:blocked", "needs:director"], remove=["status:in-progress"])
                return "blocked", [{"kind": "blocked-report", "ref": f"#{n} blocked"}], tail
            failures = self.ledger.task_failures(self.project, f"#{n}") + 1
            diagnosis = "; ".join(result.errors[-3:]) or tail or "worker ended without a PR"
            if failures >= self.cfg["breakers"]["task_failures"]:
                self.board.set_labels(n, add=["status:blocked", "needs:director"], remove=["status:in-progress"])
                self.board.comment(n, f"Attempt {failures} failed, escalating to the director instead of retrying.\n\n```\n{diagnosis[-1500:]}\n```")
            else:
                self.board.set_labels(n, add=["status:ready"], remove=["status:in-progress"])
                self.board.comment(n, f"Attempt {failures} produced no PR. Back to ready.\n\n```\n{diagnosis[-1500:]}\n```")
            return "failed", [], diagnosis
        if job.kind == "role":
            n = job.issue["number"]
            if blocked:
                self.board.set_labels(n, add=["status:blocked", "needs:director"], remove=["status:in-progress"])
                return "blocked", [{"kind": "blocked-report", "ref": f"#{n} blocked"}], tail
            if STATUS_LINE.search(result.final_text or ""):
                summary = re.sub(r"\n?STATUS:\s*DONE\s*$", "", (result.final_text or "").strip(), flags=re.I).strip()
                self.board.set_labels(n, remove=["status:in-progress"])
                self.board.close_issue(n, f"The {job.role} finished this task.\n\n{summary[-3000:]}")
                return "done", [{"kind": job.role, "ref": f"#{n} closed"}], tail
            failures = self.ledger.task_failures(self.project, f"#{n}") + 1
            diagnosis = "; ".join(result.errors[-3:]) or tail or "worker ended without a STATUS line"
            escalate = failures >= self.cfg["breakers"]["task_failures"]
            self.board.set_labels(n, add=["status:blocked", "needs:director"] if escalate else ["status:ready"],
                                  remove=["status:in-progress"])
            self.board.comment(n, f"The {job.role}'s attempt {failures} ended without a verdict"
                                  f"{', escalating to the director' if escalate else '. Back to ready'}.\n\n```\n{diagnosis[-1500:]}\n```")
            return "failed", [], diagnosis
        if job.kind == "review":
            detail = self.board.pr_detail(job.pr["number"])
            fresh = [c for c in detail.get("comments") or [] if (c.get("createdAt") or "") >= started_iso
                     and verdict_of(c.get("body"))]
            if fresh:
                verdict = verdict_of(fresh[-1]["body"])
                if verdict == "APPROVE" and not self.ci and not self.tested(detail):
                    return "failed", [], "approval without a `TESTS: PASS <head sha> <command>` line: without CI it can't merge"
                return "done", [{"kind": "review", "ref": f"PR #{job.pr['number']} {verdict}"}], tail
            return "failed", [], "; ".join(result.errors[-3:]) or "no REVIEW comment posted"
        actions = actions_of(result.final_text)
        outputs = [{"kind": job.role, "ref": str(a)[:120]} for a in actions]
        if job.kind == "audit":
            report = self.report_path()
            if report.exists():
                folder = self.reports_dir()
                folder.mkdir(parents=True, exist_ok=True)
                text = report.read_text(encoding="utf-8")
                (folder / f"{time.strftime('%Y%m%d-%H%M')}.md").write_text(text, encoding="utf-8")
                (folder / "latest.md").write_text(text, encoding="utf-8")
                outputs.insert(0, {"kind": "report", "ref": "status report"})
        return ("done" if outputs else "failed"), outputs, tail

    def prompt_text(self, key: str) -> str:
        """Project-specific guidance from room.toml `[prompts]` (commonroom#45); empty when the key isn't set."""
        return str((self.cfg.get("prompts") or {}).get(key) or "").strip()

    def headless_note(self) -> str:
        extra = self.prompt_text("headless")
        minutes = round(workers_mod.RUN_TIMEOUT / 60)
        deadline = (f" The room ends this run after {minutes} minutes. Commit as you go. Results that matter (CSV, logs, reports) "
                    f"go in a tracked path, not an ignored folder. By minute {minutes - 15}, push what you have and say on the "
                    f"issue what's left.")
        return HEADLESS_NOTE + deadline + (f" {extra}" if extra else "")

    def codex_note(self) -> str:
        """The project's Codex sandbox and tooling note (test command, engine, rendering), ending in a space."""
        extra = self.prompt_text("codex")
        return f"{extra} " if extra else ""

    def prompt(self, job: Job, branch: str | None) -> str:
        repo = self.cfg["project"]["repo"]
        if job.kind == "build":
            n = job.issue["number"]
            pr = self.board.pr_for_branch(branch)
            # Codex can't fetch or commit, so its prompt never asks it to merge main: the room handles conflicts (commonroom#51).
            conflicts = ("The room handles any conflicts with main: don't merge, fetch or copy main's files in."
                         if self._provider == "codex" else
                         "If the PR conflicts with main, first merge origin/main into the branch and resolve the "
                         "conflicts, then rerun the tests.")
            push = "the room pushes your changes to the same branch" if self._provider == "codex" else "push to the same branch"
            extra = (f"\nA PR already exists for this branch (#{pr['number']}). Read its review comments with "
                     f"`gh pr view {pr['number']} --repo {repo} --comments`, address every point, and {push}. {conflicts} "
                     f"If its CI is red (`gh pr checks {pr['number']} --repo {repo}`), "
                     f"read the failing log (`gh run view <run id> --repo {repo} --log-failed`) and fix that first."
                     if pr and pr["state"] == "OPEN" else "")
            if pr and pr["state"] == "OPEN" and merge_main_only(job.issue):
                test = (self.cfg.get("checks") or {}).get("test") or "the project's tests"
                return (f"PR #{pr['number']} in {repo} (issue #{n}) conflicts with main. This is a merge-main run, nothing "
                        f"more. You are on branch `{branch}` in a fresh worktree. Run `git fetch origin` and "
                        f"`git merge origin/main`, resolve each conflict so both sides' intent survives, run `{test}`, commit "
                        f"the merge and push with `git push origin {branch}`. Change nothing outside the conflicts: no review "
                        f"fixes, refactors or new work, so the PR needs no new review. If the tests only pass with changes "
                        f"outside the conflicts, make them in a separate commit after the merge (the PR then goes back to "
                        f"review) and say so on the PR, prefixed `[agent:builder]`.\n"
                        f"Finish with a short report, and make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")
            if job.role == "scout":
                return (f"Do the research in GitHub issue #{n} in {repo}. You are on branch `{branch}` in a fresh worktree. "
                        f"Follow your scout instructions. Commit your report under docs/scouting/, push with "
                        f"`git push -u origin {branch}`, and open a PR whose body says `Closes #{n}` and summarises the findings. "
                        f"Open any `kind:suggestion` issues the deliverable asks for. Prefix comments with `[agent:scout]`.\n"
                        f"Finish with a short report, and make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")
            if self._provider == "codex":
                return (f"Implement GitHub issue #{n} in {repo} (read it with `gh issue view {n} --repo {repo} --comments`). "
                        f"You are on branch `{branch}` in a fresh checkout of origin/main. Follow your builder instructions and "
                        f"AGENTS.md, **except for git**: your sandbox keeps .git read-only, so do not commit, push or open a PR. "
                        f"Leave your changes in the working tree, and Common Room will commit, push and open the PR for you. "
                        f"{self.codex_note()}If something in the sandbox is impossible, finish the rest and say what's left; your "
                        f"partial work is kept, not discarded.{extra}\n"
                        f"Finish with: a line `PR_TITLE: <short title>`, then `PR_BODY:` followed by each acceptance criterion and "
                        f"how you verified it, plus anything unverified. Make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")
            return (f"Implement GitHub issue #{n} in {repo}. You are already on branch `{branch}` in a fresh worktree "
                    f"based on origin/main. Follow your builder instructions and AGENTS.md. Push with "
                    f"`git push -u origin {branch}`. Prefix any issue or PR comment you write with `[agent:builder]`.{extra}\n"
                    f"Finish with a short report, and make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")
        if job.kind == "role":
            n, name = job.issue["number"], job.role
            return (f"Do the task in GitHub issue #{n} in {repo} (read it with `gh issue view {n} --repo {repo} --comments`). "
                    f"You are in a fresh detached worktree of origin/main. Follow your {name} agent instructions. Prefix every "
                    f"issue or PR comment with `[agent:{name}]`. File each finding as a new `kind:bug` issue with its evidence "
                    f"(frames, logs, numbers). Make no code PR unless your agent instructions say to. When you finish, the "
                    f"room closes issue #{n} with your report, so make the report a summary of what you found.\n"
                    f"Finish with a short report, and make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")
        if job.kind == "review":
            p = job.pr
            m = BRANCH.match(p["headRefName"])
            test = (self.cfg.get("checks") or {}).get("test") or "the project's tests"
            no_ci = ("" if self.ci else
                     f"\nThis repo has no CI: your approval is what merges this PR, so the test run is yours. Run `{test}` "
                     f"here, on the PR head, and approve only if it passes. Put this line in your comment, with the short "
                     f"sha from `git rev-parse --short HEAD`: `TESTS: PASS <sha> {test}: <the result line, e.g. Ran 64 "
                     f"tests ... OK>` (or `TESTS: FAIL <sha> {test}: <what failed>` with REVIEW: CHANGES). If `{test}` can't finish "
                     f"(a timeout, a sandbox or environment error, a missing dependency), post `REVIEW: INCOMPLETE` with "
                     f"`TESTS: INCOMPLETE <sha> {test}: <why>`, never CHANGES for that reason alone: the room reviews again. "
                     f"Real test failures stay `TESTS: FAIL` with CHANGES. An approval "
                     f"without that line, or for an older commit, does not merge.")
            return (f"Review PR #{p['number']} in {repo} (it implements issue #{m.group(1) if m else '?'}). "
                    f"The PR branch is checked out here. Follow your reviewer instructions. Post exactly one comment "
                    f"with `gh pr comment {p['number']} --repo {repo} --body ...` that starts with `[agent:reviewer] REVIEW: APPROVE` "
                    f"or `[agent:reviewer] REVIEW: CHANGES`.{no_ci}"
                    + (f"\n{self.codex_note().rstrip()}" if self._provider == "codex" and self.codex_note() else ""))
        if job.kind == "audit":
            hours = (self.cfg.get("audit") or {}).get("every_hours", 1)
            room_py = Path(__file__).resolve().parent.parent / "room.py"
            return (f"Common Room audit for {repo}. {job.reason}.\n"
                    f"Start with `python \"{room_py}\" digest --hours {max(1, hours)}`, run from this directory: it lists "
                    f"runs, merges, failures, spend, the board and usage. Follow your auditor instructions. Write the owner's "
                    f"status report to `{self.report_path()}`, exactly that path even if you move to another working copy; "
                    f"the room publishes it to the dashboard. Prefix "
                    f"every comment with `[agent:auditor]`. Any repo file change goes through a branch and PR. End with one "
                    f"line: `ACTIONS: [...]`, a JSON list of short strings naming each concrete change you made.")
        return (f"Common Room triggered a director run for {repo}. Milestone: {self.milestone}.\nTriggers: {job.reason}\n"
                f"Handle these triggers with your director instructions. Prefix every comment with `[agent:director]`. Any repo "
                f"file change must go through a branch and PR, never a push to main. End with one line: "
                f"`ACTIONS: [...]`, a JSON list of short strings naming each concrete board change you made (issues "
                f"created, closed or relabelled, PRs merged or bounced, owner tasks assigned). An empty list means you changed nothing.")

    # Worktrees -------------------------------------------------------------------------------------
    def prepare_worktree(self, job: Job, provider: str | None = None) -> tuple[Path, str | None]:
        base = self.worktree_base()
        base.mkdir(parents=True, exist_ok=True)
        # Codex's sandbox can only write inside its folder, and a worktree keeps its git metadata in the main
        # repo's .git, so Codex gets a self-contained clone (objects borrowed from the main repo via --reference).
        clone = provider == "codex"
        if job.kind == "build":
            n = job.issue["number"]
            branch = self.find_branch(n) or f"i{n}-{slug(job.issue['title'])}"
            start = f"origin/{branch}" if self.remote_head(branch) else "origin/main"
            path = base / (f"codex-{branch}" if clone else branch)
            for old in self.worktree_bases()[1:]:
                # A leftover copy in the old folder holds the branch, and `worktree add -B` would refuse it.
                if (old / path.name).exists():
                    self.cleanup_worktree(old / path.name, getattr(self, "_run_id", None))
            path = self.checkout(path, start, branch, clone)
            return path, branch
        if job.kind == "review":
            branch = job.pr["headRefName"]
            path = base / f"{'codex-' if clone else ''}review-{job.pr['number']}"
            path = self.checkout(path, f"origin/{branch}", None, clone)
            return path, branch
        path = base / f"{'codex-' if clone else ''}{job.role}-{int(time.time())}"
        path = self.checkout(path, "origin/main", None, clone)
        return path, None

    def checkout(self, path: Path, start: str, branch: str | None, clone: bool) -> Path:
        self.cleanup_worktree(path, getattr(self, "_run_id", None))
        if path.exists():
            # A process from an earlier run (e.g. a test it left running) still holds files there: use a fresh folder.
            path = path.with_name(f"{path.name}-{int(time.time())}")
        if not clone:
            if branch:
                git(["worktree", "add", "-B", branch, str(path), start], self.root)
            else:
                git(["worktree", "add", "--detach", str(path), start], self.root)
            return path
        url = git(["remote", "get-url", "origin"], self.root)
        git(["clone", "--quiet", "--reference", str(self.root), url, str(path)], self.root)
        git(["checkout", "--quiet", "-B", branch, start] if branch else ["checkout", "--quiet", "--detach", start], path)
        return path

    def find_branch(self, number: int) -> str | None:
        out = git(["branch", "-r", "--list", f"origin/i{number}-*"], self.root)
        names = [line.strip().removeprefix("origin/") for line in out.splitlines() if line.strip()]
        return names[0] if names else None

    def remote_head(self, branch: str | None) -> str | None:
        if not branch:
            return None
        try:
            git(["fetch", "--quiet", "origin", branch], self.root)
            return git(["rev-parse", f"origin/{branch}"], self.root)
        except RuntimeError:
            return None

    def cleanup_worktree(self, path: Path, run_id: str | None = None):
        """Remove a working copy, then prune git's records of missing ones. Never one that a running run still works in,
        and every removal and prune is logged with the run that caused it: an auditor's worktree vanished mid-run and
        nothing showed who removed it (commonroom#17)."""
        holder = self.ledger.worktree_in_use(path, exclude_run=run_id)
        if holder:
            self.log(f"   kept {path}: run {holder['id']} ({holder['role']} {holder['task'] or '-'}) is still working there")
            return
        if path.exists() and (path / ".git").is_dir():  # a self-contained clone (Codex jobs)
            import shutil, stat
            self.log(f"   removing clone {path} (run {run_id or '-'})")
            shutil.rmtree(path, onerror=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))
        elif path.exists():
            self.log(f"   removing worktree {path} (run {run_id or '-'})")
            try:
                git(["worktree", "remove", "--force", str(path)], self.root)
            except RuntimeError as exc:
                self.log(f"   could not fully remove {path}: {str(exc)[:200]}")
        # `prune -v` names what it drops on stderr: a worktree whose folder or .git link someone else deleted.
        done = subprocess.run(["git", "worktree", "prune", "-v"], cwd=self.root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=git_env(self.root))
        pruned = [line.strip() for line in (done.stderr or "").splitlines() if line.strip()]
        if pruned:
            self.log(f"   git worktree prune (run {run_id or '-'}): " + "; ".join(pruned)[:400])

    # Budget ----------------------------------------------------------------------------------------
    def weekly_used(self, provider: str = "claude") -> float | None:
        row = self.ledger.latest_usage(provider).get("seven_day")
        return row["used_pct"] if row else None

    def five_hour_used(self, provider: str) -> float | None:
        row = self.ledger.latest_usage(provider).get("five_hour")
        return row["used_pct"] if row else None

    def stop_reason(self, provider: str) -> str | None:
        """A worker already running finishes its job unless a reserve is hit, a hard pause is set or an automatic
        breaker trips. A pause, the disk guard, pace and the owner's own use hold new starts only (commonroom#23)."""
        v = verdicts(self.ledger, self.cfg)[provider]
        return v.reason if not v.allowed and v.stop_running else None

    # One pass --------------------------------------------------------------------------------------
    def refresh_readings(self, now: float):
        """Re-read stale meters without inference. Failures just leave the reading stale (the gate holds)."""
        limit = self.cfg["freshness_seconds"] * 0.6
        failed = getattr(self, "_probe_failed", {})
        self._probe_failed = failed
        for provider, reader in (("codex", usage.codex_limits), ("claude", usage.claude_screen_limits)):
            rows = self.ledger.latest_usage(provider)
            if rows and all(now - r["observed_at"] < limit for r in rows.values()):
                continue
            if now - failed.get(provider, 0) < 1800:
                continue  # a probe that just failed will fail again; workers' own events keep readings fresh
            try:
                self.ledger.record_usage(reader())
                failed.pop(provider, None)
            except Exception as exc:
                failed[provider] = now
                self.log(f"   {provider} meter refresh failed (retrying in 30 min): {str(exc)[:160]}")

    def recover_orphans(self, now: float | None = None) -> list[str]:
        """A dispatcher that died mid-job (PC shut down, crash) leaves its run 'running' and its issue in progress,
        which no one would ever pick up again. Close the run as stopped (not an attempt), save any unsaved work
        from its working copy onto the branch, and put the task back."""
        now = time.time() if now is None else now
        actions = []
        for run in self.ledger.running_runs(self.project):
            if not orphaned(run, now):
                continue
            if self.dry_run or not self.ledger.close_orphan(run["id"], "dispatcher died mid-run (shutdown or crash); work saved, task requeued", now):
                continue
            task = run["task"] or ""
            actions.append(f"recovered {run['role']}/{run['provider']} {task or '-'} left running by a dead dispatcher")
            m = re.fullmatch(r"#(\d+)", task)
            if not m:
                continue   # reviews, director and audit runs simply run again when due
            n = int(m.group(1))
            issue = next((i for i in self.board.open_issues(None) if i["number"] == n), None)
            if self.root:
                for path in [p for base in self.worktree_bases() for p in (*base.glob(f"i{n}-*"), *base.glob(f"codex-i{n}-*"))]:
                    if self.ledger.worktree_in_use(path, exclude_run=run["id"]):
                        continue   # a live run took the task over: its working copy is not the orphan's
                    branch = path.name.removeprefix("codex-")
                    if issue:
                        self.preserve_work(Job("build", run["role"], issue=issue), path, branch)
                    self.cleanup_worktree(path, run["id"])
            if issue and "status:in-progress" in labels(issue):
                self.board.set_labels(n, add=["status:ready"], remove=["status:in-progress"])
                self.board.comment(n, "The worker was interrupted (the PC shut down or its dispatcher crashed). "
                                      "Back to ready; this does not count as an attempt.")
        return actions

    def run_once(self) -> dict:
        self.refresh_readings(time.time())
        for line in self.recover_orphans():
            self.log("   " + line)
        report = {"housekeeping": (self.locked_housekeeping() or []) if self.only_provider in (None, "claude") and self.slot == 1 else []}
        for line in report["housekeeping"]:
            self.log("   " + line)
        now = time.time()
        limit = float((self.cfg.get("breakers") or {}).get("progress_spend_limit", 10.0))
        trip = check_progress(self.ledger, self.project, now, spend_limit=limit,
                              attribute=(self.cfg.get("share") or {}).get("max") is not None)
        if trip:
            self.ledger.trip(self.project, trip["kind"], trip["detail"], now=now)
            self.escalate_breaker(trip)
        allowed = self.allowed(now)
        report["verdicts"] = {p: v.reason for p, v in allowed.items()}
        picked = self.pick(allowed)
        if not picked:
            report["job"] = None
            self.log("   nothing to run: " + "; ".join(v.reason for v in allowed.values()))
            return report
        job, provider, model = picked
        report["job"] = {"kind": job.kind, "provider": provider, "model": model, "reason": job.reason,
                         **self.run_job(job, provider, model)}
        return report

    @property
    def housekeeping_lock(self) -> Path:
        return Path(self.ledger.path).parent / f"{self.project or 'default'}-housekeep.lock"

    def housekeeping_locked(self) -> bool:
        """Does another live process hold the housekeeping lock? (A lock of our own pid or a dead one is free.)"""
        path = self.housekeeping_lock
        if not path.exists():
            return False
        free = acquire_lock(path)
        if free:
            path.unlink(missing_ok=True)
        return not free

    def locked_housekeeping(self) -> list[str] | None:
        """One housekeeping pass per project at a time (one merge per pass: commonroom#19). None if another holds it."""
        if self.dry_run:
            return self.safe_housekeeping()
        path = self.housekeeping_lock
        if not acquire_lock(path):
            self.log(f"   housekeeping skipped: another pass holds {path.name}")
            return None
        try:
            return self.safe_housekeeping()
        finally:
            path.unlink(missing_ok=True)

    def safe_housekeeping(self) -> list[str]:
        """Housekeeping, but an exception in it must not stop the pass: the project's builds run the same code."""
        try:
            return self.housekeeping()
        except Exception as exc:   # KeyboardInterrupt and SystemExit are not Exceptions and still propagate
            trace = traceback.format_exc()
            self.log(trace)
            self.escalate_housekeeping(exc, trace)
            return [f"housekeeping failed: {type(exc).__name__}: {exc}"]

    def escalate_housekeeping(self, exc: Exception, trace: str):
        if self.dry_run:
            return
        title = f"[room] Housekeeping is failing: {type(exc).__name__}"
        try:
            if any(i["title"] == title for i in self.board.open_issues(None)):
                return
            tail = "\n".join(trace.strip().splitlines()[-12:])
            body = ("Housekeeping raised in the dispatcher pass, so it was skipped and the pass carried on.\n\n"
                    f"**Message:** {exc}\n\n```\n{tail}\n```\n\nDue: {time.strftime('%Y-%m-%d')}")
            self.board.create_issue(title, body, labels=("needs:director",))
        except Exception as err:   # the board may be the very thing that is broken
            self.log(f"   could not open the housekeeping issue: {err}")

    def escalate_breaker(self, trip: dict):
        if self.dry_run:
            return
        title = f"[You] Room stopped: {trip['kind'].replace('_', ' ')}"
        existing = [i for i in self.board.open_issues(None) if i["title"] == title]
        if existing:
            return
        room_py = (Path(self.room_root) / "room.py").as_posix()
        body = (f"Common Room stopped autonomous work.\n\n**Why:** {trip['detail']}\n\nDue: {time.strftime('%Y-%m-%d')}\n\n"
                f"**What to do:** read the recent runs (`python {room_py} status`) and the run logs, then "
                f"decide in an executive session what changes. Clear the stop with `python {room_py} breaker reset`.")
        number = self.board.create_issue(title, body, labels=("owner:human",), assignees=(self.owner,) if self.owner else ())
        project = self.cfg["project"].get("name") or ""
        # Pushed at once, at high priority; the owner-task pass then knows it was.
        if self.tell_owner(f"{project}: room stopped ({trip['kind'].replace('_', ' ')})", trip["detail"],
                           url=self.issue_url(number), priority=4):
            self.ledger.mark(self.project, f"owner-task:{number}")


def steer_reason(cfg: dict) -> str:
    """STEER_REASON, naming the Director log issue when `[director] log_issue` sets one (a number, or `owner/repo#N`)."""
    log = (cfg.get("director") or {}).get("log_issue")
    if not log:
        return STEER_REASON
    return f"{STEER_REASON} ({f'#{log}' if isinstance(log, int) else log})"


def schedule_due(spec: dict, last: float | None, now: float) -> bool:
    """A `[schedule.<role>]` is due when the latest passed `at` time (local "HH:MM", one or a list) came after the
    last issue it opened, or `every_hours` have passed since then. Never opened: due at once."""
    at = spec.get("at")
    if at:
        _, slot, _ = bulletin_window([at] if isinstance(at, str) else list(at), now)
        return last is None or last < slot
    every = float(spec.get("every_hours") or 0)
    return bool(every) and (last is None or now - last >= every * 3600)


def fill(template: str, now: float) -> str:
    """`{date}` (YYYY-MM-DD) and `{weekday}` in a schedule's title or body, local time; other braces are left alone."""
    local = time.localtime(now)
    return template.replace("{date}", time.strftime("%Y-%m-%d", local)).replace("{weekday}", time.strftime("%A", local))


def bulletin_window(deadlines: list[str], now: float) -> tuple[float, float, str]:
    """(previous deadline, latest passed deadline, its HH:MM) for local `HH:MM` deadlines, looking back two days."""
    today = datetime.datetime.fromtimestamp(now).date()
    stamps = []
    for back in (2, 1, 0):
        day = today - datetime.timedelta(days=back)
        for text in deadlines:
            hour, minute = (int(part) for part in str(text).split(":", 1))
            stamps.append((datetime.datetime.combine(day, datetime.time(hour, minute)).timestamp(), f"{hour:02d}:{minute:02d}"))
    passed = sorted(s for s in stamps if s[0] <= now)
    return passed[-2][0], passed[-1][0], passed[-1][1]


def _after(comment: dict, since: float) -> bool:
    created = comment.get("createdAt") or ""
    try:
        return calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) > since
    except ValueError:
        return False


def _stamp(iso: str | None) -> float:
    """Seconds since the epoch for a GitHub timestamp; 0 when there is none."""
    try:
        return float(calendar.timegm(time.strptime(str(iso or "")[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0
