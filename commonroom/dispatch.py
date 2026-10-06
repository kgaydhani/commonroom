"""Turn the board into work. Deterministic: decides what runs; models only do the work.

One pass (`run_once`):
1. Housekeeping with no model calls: merge approved green PRs, unblock issues whose dependencies closed.
2. Pick one piece of model work, highest value first: review a waiting PR, build a ready issue, or run the
   director on a trigger. Each needs an allowed provider for its role.
3. Run it, record usage and outputs, update the board, check breakers.
"""
from __future__ import annotations

import calendar
import os
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from commonroom import board as board_mod, config as config_mod, gate, usage
from commonroom.ledger import Ledger
from commonroom.workers import load_role, run_worker

DEPENDS = re.compile(r"^(?:Depends on|Blocked by)\b(.*)$", re.I | re.M)
BRANCH = re.compile(r"^i(\d+)-")
MARK = "[room]"
STATUS_LINE = re.compile(r"STATUS:\s*(DONE|BLOCKED)", re.I)
ACTIONS_LINE = re.compile(r"ACTIONS:\s*(\[.*\])", re.S)
DIRECTOR_COOLDOWN = 3600
OWNER_COOLDOWN = 300
SCOUT_INTERLEAVE = 3 * 3600  # a ready research task jumps the build queue at most this often


def priority(issue: dict) -> tuple:
    """prio:top (foundation work others build on, e.g. a milestone plan) first, then prio:high, prio:low last,
    otherwise oldest first."""
    names = labels(issue)
    return (-1 if "prio:top" in names else 0 if "prio:high" in names else 2 if "prio:low" in names else 1, issue["number"])


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")


def labels(issue: dict) -> set[str]:
    return {l["name"] for l in issue.get("labels", [])}


def dependencies(body: str | None) -> set[int]:
    deps = set()
    for line in DEPENDS.findall(body or ""):
        deps |= {int(n) for n in re.findall(r"#(\d+)", line)}
    return deps


def find_bash() -> str | None:
    """Git for Windows' bash. Checked before PATH, which can hold WSL's bash.exe and which is minimal under Task Scheduler."""
    import shutil
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists() and "system32" not in candidate.lower():
            return candidate
    return None


def git(args: list[str], cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


class GitHubBoard:
    """Board operations through `gh`. Swappable for a fake in tests."""

    def __init__(self, repo: str):
        self.repo = repo

    def open_issues(self, milestone: str | None) -> list[dict]:
        return board_mod.issues(self.repo, milestone=milestone)

    def all_issues(self) -> list[dict]:
        return board_mod.gh(["issue", "list", "--repo", self.repo, "--state", "all", "--limit", "500",
                             "--json", "number,state,labels,title,body,comments,updatedAt"]) or []

    def set_labels(self, number: int, add=(), remove=()):
        args = ["issue", "edit", str(number), "--repo", self.repo]
        for name in add:
            args += ["--add-label", name]
        for name in remove:
            args += ["--remove-label", name]
        if len(args) > 4:
            board_mod.gh(args)

    def comment(self, number: int, body: str):
        board_mod.gh(["issue", "comment", str(number), "--repo", self.repo, "--body", f"{MARK} {body}"])

    def open_prs(self) -> list[dict]:
        return board_mod.gh(["pr", "list", "--repo", self.repo, "--state", "open", "--limit", "100",
                             "--json", "number,headRefName,url,title,labels"]) or []

    def pr_detail(self, number: int) -> dict:
        return board_mod.gh(["pr", "view", str(number), "--repo", self.repo,
                             "--json", "number,state,headRefName,url,commits,comments,statusCheckRollup,mergeable"])

    def pr_for_branch(self, branch: str) -> dict | None:
        prs = board_mod.gh(["pr", "list", "--repo", self.repo, "--head", branch, "--state", "all",
                            "--json", "number,state,url,headRefName"]) or []
        return prs[0] if prs else None

    def label_pr(self, number: int, name: str):
        board_mod.gh(["pr", "edit", str(number), "--repo", self.repo, "--add-label", name])

    def merge(self, number: int):
        board_mod.gh(["pr", "merge", str(number), "--repo", self.repo, "--squash", "--delete-branch"])

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


# PR state ----------------------------------------------------------------------------------------
def latest_review(detail: dict) -> str | None:
    """The newest REVIEW verdict posted after the PR's latest commit, or None if it needs (re)review."""
    commits = detail.get("commits") or []
    last_commit = max((c.get("committedDate") or "" for c in commits), default="")
    verdict = None
    for c in detail.get("comments") or []:
        body = (c.get("body") or "").strip()
        if (c.get("createdAt") or "") >= last_commit:
            m = re.search(r"REVIEW:\s*(APPROVE|CHANGES)", body)
            if m:
                verdict = m.group(1)
    return verdict


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


def failed_checks(detail: dict) -> list[dict]:
    return [c for c in detail.get("statusCheckRollup") or []
            if (c.get("conclusion") or c.get("state") or "").upper() in ("FAILURE", "ERROR", "ACTION_REQUIRED", *INFRA_CONCLUSIONS)]


def ci_infra_only(detail: dict) -> bool:
    """Red only because CI never really ran (runner outage, timeout, cancellation): rerun, don't blame the builder."""
    failed = failed_checks(detail)
    return bool(failed) and all((c.get("conclusion") or c.get("state") or "").upper() in INFRA_CONCLUSIONS for c in failed)


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
    return [c for c in issue.get("comments") or [] if not (c.get("body") or "").lstrip().startswith("[")]


# Policy ------------------------------------------------------------------------------------------
def verdicts(ledger: Ledger, cfg: dict, now: float | None = None, trip: bool = False) -> dict[str, gate.Verdict]:
    """Only the dispatcher records breaker trips (`trip=True`). Views (status, dashboard) are read-only, so a
    long-running view on old code can never re-trip a breaker the current code would not."""
    now = time.time() if now is None else now
    project = cfg["project"].get("name")
    n = cfg["breakers"]["no_output_runs"]
    result = {}
    for provider in ("claude", "codex"):
        # A provider that keeps producing nothing is held on its own; the other keeps working.
        # Breakers named `kind:provider` hold that provider only; owner/auditor pauses and no_progress hold everything.
        runs = ledger.recent_runs(project, limit=n, autonomous=True, finished=True, provider=provider)
        mine = [b for b in ledger.open_breakers(project) if ":" not in b["kind"] or b["kind"].endswith(":" + provider)]
        breaker, tripped = gate.breaker_verdict(mine, runs, n)
        if tripped and trip:
            ledger.trip(project, f"{tripped['kind']}:{provider}", tripped["detail"], now=now)
        v = gate.usage_verdict(provider, ledger.latest_usage(provider), config_mod.budget(cfg, provider), now,
                               cfg["freshness_seconds"])
        cool = ledger.cooldown(provider, now)
        if v.allowed and cool:
            v = gate.Verdict(False, f"{provider}: {cool['reason']}; resting until "
                                    f"{time.strftime('%H:%M', time.localtime(cool['until']))}", v.detail)
        result[provider] = v if (not v.allowed or breaker.allowed) else gate.Verdict(False, breaker.reason, v.detail)
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


def check_progress(ledger: Ledger, project: str | None, now: float, spend_limit: float = 10.0) -> dict | None:
    """Progress guarantee: >spend_limit weekly-% of autonomous Claude spend in 24h with no merge trips a breaker."""
    since = now - 86400
    runs = [r for r in ledger.recent_runs(project, limit=500) if r["started_at"] >= since]
    spent = sum(max(0.0, (r.get("weekly_end") or 0) - (r.get("weekly_start") or 0))
                for r in runs if r["provider"] == "claude" and r["autonomous"] and r.get("weekly_start") is not None)
    merged = sum(1 for r in runs if r["role"] == "merge")
    if spent > spend_limit and merged == 0:
        return {"kind": "no_progress", "detail": f"{spent:.1f}% of the weekly Claude allowance spent by autonomous runs in 24h with nothing merged"}
    return None


# The dispatcher ----------------------------------------------------------------------------------
@dataclass
class Job:
    kind: str           # build | review | direct
    role: str
    issue: dict | None = None
    pr: dict | None = None
    reason: str = ""


class Dispatcher:
    def __init__(self, cfg: dict, ledger: Ledger, board=None, log=print, dry_run: bool = False, only_provider: str | None = None,
                 slot: int = 1):
        self.cfg, self.ledger, self.log, self.dry_run = cfg, ledger, log, dry_run
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

    # Housekeeping (no model) -----------------------------------------------------------------------
    def housekeeping(self) -> list[str]:
        actions = []
        everything = self.board.all_issues()
        state = {i["number"]: i["state"] for i in everything}
        for pr in self.board.open_prs():
            m = BRANCH.match(pr["headRefName"])
            if not m:
                continue
            detail = self.board.pr_detail(pr["number"])
            review, checks = latest_review(detail), checks_state(detail)
            issue_no = int(m.group(1))
            # A PR that conflicts with main can't merge (and CI may never run): send it back to its builder.
            if detail.get("mergeable") == "CONFLICTING" and not self.dry_run:
                issue = next((i for i in everything if i["number"] == issue_no), None)
                if issue and issue["state"] == "OPEN" and "status:ready" not in labels(issue):
                    self.board.set_labels(issue_no, add=["status:ready", "needs:changes"], remove=["status:review", "status:in-progress"])
                    self.board.comment(issue_no, f"PR #{pr['number']} conflicts with main. The next builder run merges "
                                                 f"origin/main into the branch, resolves the conflicts and reruns the tests.")
                    actions.append(f"PR #{pr['number']} has merge conflicts; issue #{issue_no} back to its builder")
                continue
            # Red CI: rerun what never really ran; send real failures back to the builder (never let a PR sit red).
            if checks == "red" and not self.dry_run:
                actions += self.handle_red_ci(pr, detail, issue_no, everything)
                continue
            # Research reports (docs only) skip review: CI green is enough.
            if "review:skip" in labels(pr) and checks == "green":
                review = "APPROVE"
            if review == "APPROVE" and checks == "green":
                if not self.dry_run:
                    try:
                        self.board.merge(pr["number"])
                    except Exception as exc:  # e.g. another dispatcher merged it a moment ago
                        actions.append(f"merge of PR #{pr['number']} skipped: {str(exc)[:120]}")
                        continue
                    run = self.ledger.start_run(self.project, f"#{issue_no}", "merge", "none")
                    self.ledger.finish_run(run, "done", outputs=[{"kind": "merge", "ref": f"PR #{pr['number']}"}])
                actions.append(f"merged PR #{pr['number']} (issue #{issue_no})")
                state[issue_no] = "CLOSED"
            elif review == "CHANGES" and not self.dry_run:
                issue = next((i for i in everything if i["number"] == issue_no), None)
                if issue and "status:ready" not in labels(issue):
                    self.board.set_labels(issue_no, add=["status:ready", "needs:changes"], remove=["status:review", "status:in-progress"])
                    actions.append(f"PR #{pr['number']} needs changes; issue #{issue_no} back to ready")
        if any(a.startswith("merged") for a in actions) and not self.dry_run:
            actions += self.deploy()
        for issue in everything:
            names = labels(issue)
            if issue["state"] != "OPEN" or "status:blocked" not in names or names & {"needs:director", "owner:human"}:
                continue
            deps = dependencies(issue.get("body"))
            if deps and all(state.get(d) == "CLOSED" for d in deps):
                if not self.dry_run:
                    self.board.set_labels(issue["number"], add=["status:ready"], remove=["status:blocked"])
                actions.append(f"unblocked #{issue['number']} (dependencies {sorted(deps)} closed)")
        return actions

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
                    from commonroom import services
                    restarted = services.restart_after_deploy(self.cfg, log_dir)
                    return [f"deploy ok{' (after a retry)' if attempt == 2 else ''}" + (f"; {', '.join(restarted)}" if restarted else "")]
            tail = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
            return [f"deploy FAILED twice: {tail[0][:120]} (full log: {log_path})"]
        except Exception as exc:
            return [f"deploy FAILED: {type(exc).__name__}: {str(exc)[:160]}"]

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
        # Only once the builder has finished (status:review); a builder still pushing will retrigger CI itself.
        if not issue or issue["state"] != "OPEN" or "status:review" not in labels(issue):
            return []
        names = ", ".join(sorted({c.get("name") or c.get("context") or "check" for c in failed}))
        bounces = sum(1 for c in issue.get("comments") or [] if "CI is red" in (c.get("body") or ""))
        if bounces >= CI_BOUNCES:
            self.board.set_labels(issue_no, add=["status:blocked", "needs:director"], remove=["status:review"])
            self.board.comment(issue_no, f"CI is red again on PR #{pr['number']} ({names}) after {bounces} fixes. Escalating to the director.")
            return [f"PR #{pr['number']} still red after {bounces} fixes; #{issue_no} escalated"]
        self.board.set_labels(issue_no, add=["status:ready", "needs:changes"], remove=["status:review", "status:in-progress"])
        self.board.comment(issue_no, f"CI is red on PR #{pr['number']}: {names}. Read the failure with "
                                     f"`gh pr checks {pr['number']}` and `gh run view <run id> --log-failed`, fix it on the same "
                                     f"branch, and run `bash tools/test.sh` before pushing.")
        return [f"PR #{pr['number']} CI red ({names}); #{issue_no} back to its builder"]

    # Choosing work ---------------------------------------------------------------------------------
    def choose(self, spec: list[str], allowed: dict[str, gate.Verdict], exclude: str | None = None):
        choice = choose_provider(spec, allowed, exclude=exclude) or (choose_provider(spec, allowed) if exclude else None)
        if choice and self.only_provider and choice[0] != self.only_provider:
            return None
        return choice

    def ready_builds(self, open_now: list[dict]) -> list[dict]:
        running = self.ledger.running_tasks(self.project)
        return [i for i in open_now if {"status:ready", "role:builder"} <= labels(i) and "owner:human" not in labels(i)
                and f"#{i['number']}" not in running
                and (not self.milestone or (i.get("milestone") or {}).get("title") == self.milestone
                     or any(l.startswith("provider:") for l in labels(i)))]

    def build_spec(self, issue: dict) -> list[str]:
        builder_spec = self.roles.get("builder", ["claude:sonnet"])
        # `provider:codex` / `provider:claude` pins a task to the subscription that has the needed capability.
        pinned = [l.split(":", 1)[1] for l in labels(issue) if l.startswith("provider:")]
        # Large, high-risk and integration work goes to the heavy builder (Opus); routine work to the standard one.
        heavy = labels(issue) & {"size:L", "risk:high", "builder:heavy"}
        return pinned or (self.roles.get("builder_heavy", builder_spec) if heavy else builder_spec)

    def pick_extra(self, allowed: dict[str, gate.Verdict]) -> tuple[Job, str, str | None] | None:
        """An extra slot for the same provider: builds only (no reviews, director or audits, which slot 1 owns),
        only while slot 1 is busy, the queue is deep, and weekly use sits well under the pace line."""
        par = self.cfg.get("parallel") or {}
        provider = self.only_provider or "claude"
        v = allowed.get(provider)
        if self.slot > int(par.get(f"{provider}_slots", 1)) or not v or not v.allowed:
            return None
        pace, used = v.detail.get("weekly_pace"), v.detail.get("weekly_used")
        if pace is None or used is None or pace - used < par.get("min_pace_headroom", 15):
            return None
        busy = any(r["provider"] == provider and r["status"] == "running" for r in self.ledger.recent_runs(self.project, limit=20))
        ready = sorted(self.ready_builds(self.board.open_issues(None)), key=priority)
        if not busy or len(ready) < par.get("min_ready", 3):
            return None
        for issue in ready:
            choice = self.choose(self.build_spec(issue), {provider: v})
            if choice:
                return Job("build", "builder", issue=issue, reason=f"#{issue['number']} is ready (parallel slot {self.slot})"), *choice
        return None

    def pick(self, allowed: dict[str, gate.Verdict]) -> tuple[Job, str, str | None] | None:
        if self.slot > 1:
            return self.pick_extra(allowed)
        builder_spec = self.roles.get("builder", ["claude:sonnet"])
        issue_labels = None
        running = self.ledger.running_tasks(self.project)
        for pr in self.board.open_prs():
            m = BRANCH.match(pr["headRefName"])
            if not m or "review:skip" in labels(pr) or f"PR #{pr['number']}" in running:
                continue
            detail = self.board.pr_detail(pr["number"])
            if latest_review(detail) is None and checks_state(detail) != "red":
                if issue_labels is None:
                    issue_labels = {i["number"]: labels(i) for i in self.board.all_issues()}
                # Codex (the smaller subscription) is spent where independence matters most: high-risk changes.
                high_risk = "risk:high" in issue_labels.get(int(m.group(1)), set())
                spec = self.roles.get("reviewer_high_risk" if high_risk else "reviewer", builder_spec)
                built_by = self.last_provider(f"#{m.group(1)}", "builder")
                choice = self.choose(spec, allowed, exclude=built_by)
                if choice:
                    return Job("review", "reviewer", pr=detail, reason=f"PR #{pr['number']} awaits {'high-risk ' if high_risk else ''}review"), *choice
        open_now = self.board.open_issues(None)
        ready = self.ready_builds(open_now)
        scouting = [i for i in open_now if {"status:ready", "role:scout"} <= labels(i)]
        # The owner waiting on an answer outranks builds: a reply should never sit behind the whole queue.
        owner = self.owner_trigger()
        if owner:
            choice = self.choose(self.roles.get("director", ["claude:opus"]), allowed)
            if choice:
                return Job("direct", "director", reason=owner), *choice
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
        pinned_here = [i for i in ready if f"provider:{self.only_provider}" in labels(i)] if self.only_provider else []
        if self.only_provider == "codex" and self.roles.get("codex_parallel_builds", True) and not pinned_here:
            # Work only Codex can do (pinned, e.g. image generation) comes before routine parallel builds.
            # Codex shouldn't idle while there's a queue: it takes the second routine task in parallel,
            # leaving the top-priority one for Claude, and never the heavy (Opus) work.
            # Codex's sandbox can't render (no Godot windows, no browser), so visual and perf work stays with Claude.
            routine = [i for i in sorted(ready, key=priority)
                       if not labels(i) & {"size:L", "risk:high", "builder:heavy", "kind:perf", "kind:art", "needs:render"}
                       and not any(l.startswith("provider:") for l in labels(i))]
            claude_busy = any(r["provider"] == "claude" and r["status"] == "running"
                              for r in self.ledger.recent_runs(self.project, limit=20))
            # Leave the top routine task for Claude, unless Claude is already busy on something else.
            take = routine[0] if (routine and claude_busy) else (routine[1] if len(routine) >= 2 else None)
            if take and allowed.get("codex") and allowed["codex"].allowed:
                return Job("build", "builder", issue=take, reason=f"#{take['number']} is ready (parallel Codex build)"), "codex", None
        for issue in sorted(ready, key=priority):
            choice = self.choose(self.build_spec(issue), allowed)
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
        trigger = self.director_trigger()
        if trigger:
            choice = self.choose(self.roles.get("director", ["claude:opus"]), allowed)
            if choice:
                return Job("direct", "director", reason=trigger), *choice
        return None

    def last_run_at(self, role: str) -> float:
        return next((r["started_at"] for r in self.ledger.recent_runs(self.project, limit=200) if r["role"] == role), 0.0)

    def reports_dir(self) -> Path:
        return Path(self.ledger.path).parent / "reports" / (self.project or "default")

    def audit_due(self) -> str | None:
        """Every `[audit] every_hours` if anything happened since the last audit (or every 6h regardless)."""
        audit = self.cfg.get("audit") or {}
        if not audit or not audit.get("enabled", True):  # opt-in per project via [audit] in room.toml
            return None
        every = float(audit.get("every_hours", 1))
        last = self.last_run_at("auditor")
        age = time.time() - last
        if age < every * 3600:
            return None
        activity = [r for r in self.ledger.recent_runs(self.project, limit=200)
                    if r["started_at"] > last and r["role"] not in ("auditor",)]
        if not activity and age < 6 * 3600:
            return None
        # Fable has its own weekly window: keep a quarter of it for the owner.
        fable = self.ledger.latest_usage("claude").get("seven_day_fable")
        if fable and fable["used_pct"] > 75:
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
        body = (f"Scheduled capability sweep ({today}). Focus: **{focus}**.\n\n"
                "Find tools, repos, MCP servers, techniques or references that are free (or already paid for), available on "
                "this machine (docs/environment.md), and would make the game better or the studio faster, cheaper or more capable. "
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
        for issue in self.board.open_issues(None):
            names = labels(issue)
            comments = issue.get("comments") or []
            # Only unanswered: the last word must be the owner's, and new since the last director run.
            last_is_owner = bool(comments) and not (comments[-1].get("body") or "").lstrip().startswith("[")
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
        open_issues = self.board.open_issues(self.milestone) if self.milestone else []
        for issue in open_issues:
            names = labels(issue)
            if "needs:director" in names:
                reasons.append(f"#{issue['number']} escalated to the director")
            if "kind:suggestion" in names and "triaged" not in names:
                reasons.append(f"#{issue['number']} is an untriaged owner suggestion")
            if "owner:human" in names and any(_after(c, since) for c in owner_comments(issue)):
                reasons.append(f"#{issue['number']} has a new owner comment (delivery or answer)")
        busy = [i for i in open_issues if labels(i) & {"status:ready", "status:in-progress", "status:review"}]
        owner_open = {i["number"] for i in open_issues if "owner:human" in labels(i)}
        # Work that only waits on the owner is not a reason to wake the director.
        machine_open = [i for i in open_issues if "owner:human" not in labels(i)
                        and not (dependencies(i.get("body")) & owner_open)]
        if machine_open and not busy:
            reasons.append("the milestone has open work but nothing is ready: unblock, re-plan or split tasks")
        return "; ".join(reasons) or None

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
        run_id = self.ledger.start_run(self.project, task, job.role, provider, model or role.model, exclusive=bool(task))
        if run_id is None:
            self.log(f"   {task} is already being worked on; skipped")
            return {"status": "skipped", "outputs": [], "note": "already running"}
        try:
            git(["fetch", "--quiet", "origin"], self.root)
            worktree, branch = self.prepare_worktree(job, provider)
        except Exception as exc:
            self.ledger.finish_run(run_id, "room-error", [], note=f"{type(exc).__name__}: {exc}"[:800])
            raise
        labelled = False
        try:
            before = self.remote_head(branch)
            weekly_start = self.weekly_used()
            if job.issue:
                self.board.set_labels(job.issue["number"], add=["status:in-progress"], remove=["status:ready", "needs:changes"])
                labelled = True
            started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._worktree, self._provider = worktree, provider
            result = run_worker(provider, role, self.prompt(job, branch), worktree, self.ledger, self.log_dir,
                                model=model, effort="high" if job.role in ("director", "auditor") else None,
                                stop_check=lambda: self.stop_reason(provider))
            if result.limited_until or result.interrupted:
                status, outputs, note = self.requeue(job, provider, result)
            else:
                if provider == "codex" and job.kind == "build":
                    self.publish_sandboxed_work(job, worktree, branch, result)
                status, outputs, note = self.assess(job, branch, before, started_iso, result)
            self.ledger.finish_run(run_id, status, outputs, result.input_tokens, result.output_tokens, result.cost_usd,
                                   note, session_id=result.session_id)
            self.ledger.db.execute("UPDATE runs SET weekly_start=?, weekly_end=? WHERE id=?",
                                   (weekly_start, self.weekly_used(), run_id))
            self.ledger.db.commit()
        except Exception as exc:
            # A room-side error is not a model failure: put the task back and record the error honestly.
            if run_id:
                self.ledger.finish_run(run_id, "room-error", [], note=f"{type(exc).__name__}: {exc}"[:800])
            if labelled:
                try:
                    self.board.set_labels(job.issue["number"], add=["status:ready"], remove=["status:in-progress"])
                except Exception:
                    pass
            raise
        finally:
            self.preserve_work(job, worktree, branch)
            self.cleanup_worktree(worktree)
        self.log(f"   {status}: {', '.join(o['ref'] for o in outputs) or 'no output'}  {note[:200]}")
        return {"status": status, "outputs": outputs, "note": note, "session": result.session_id}

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
        try:
            if not git(["status", "--porcelain"], worktree):
                return
            git(["add", "-A"], worktree)
            git(["commit", "-q", "-m", "WIP: unfinished work preserved by Common Room\n\nThe worker stopped before "
                 "finishing; the next attempt continues from this commit."], worktree)
            git(["push", "-q", "-u", "origin", branch], worktree)
            if job.issue:
                self.board.comment(job.issue["number"], f"Unfinished work was preserved on branch `{branch}`. "
                                                        f"The next attempt continues from it.")
        except Exception as exc:
            self.log(f"   could not preserve work on {branch}: {str(exc)[:160]}")

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

    def assess(self, job: Job, branch: str | None, before: str | None, started_iso: str, result) -> tuple[str, list, str]:
        tail = (result.final_text or "").strip()[-600:]
        blocked = bool(re.search(r"STATUS:\s*BLOCKED", result.final_text or "", re.I))
        if job.kind == "build":
            n = job.issue["number"]
            pr = self.board.pr_for_branch(branch)
            after = self.remote_head(branch)
            if pr and pr["state"] == "OPEN" and after and after != before:
                self.board.set_labels(n, add=["status:review"], remove=["status:in-progress"])
                if job.role == "scout" and hasattr(self.board, "label_pr"):
                    self.board.label_pr(pr["number"], "review:skip")
                return "done", [{"kind": "pr", "ref": f"PR #{pr['number']}"}], tail
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
        if job.kind == "review":
            detail = self.board.pr_detail(job.pr["number"])
            fresh = [c for c in detail.get("comments") or [] if (c.get("createdAt") or "") >= started_iso
                     and re.search(r"REVIEW:\s*(APPROVE|CHANGES)", c.get("body") or "")]
            if fresh:
                verdict = re.search(r"REVIEW:\s*(APPROVE|CHANGES)", fresh[-1]["body"]).group(1)
                return "done", [{"kind": "review", "ref": f"PR #{job.pr['number']} {verdict}"}], tail
            return "failed", [], "; ".join(result.errors[-3:]) or "no REVIEW comment posted"
        m = ACTIONS_LINE.search(result.final_text or "")
        try:
            actions = json.loads(m.group(1)) if m else []
        except ValueError:
            actions = []
        outputs = [{"kind": job.role, "ref": str(a)[:120]} for a in actions]
        if job.kind == "audit":
            report = Path(getattr(self, "_worktree", ".")) / ".room-report.md"
            if report.exists():
                folder = self.reports_dir()
                folder.mkdir(parents=True, exist_ok=True)
                text = report.read_text(encoding="utf-8")
                (folder / f"{time.strftime('%Y%m%d-%H%M')}.md").write_text(text, encoding="utf-8")
                (folder / "latest.md").write_text(text, encoding="utf-8")
                outputs.insert(0, {"kind": "report", "ref": "status report"})
        return ("done" if outputs else "failed"), outputs, tail

    def prompt(self, job: Job, branch: str | None) -> str:
        repo = self.cfg["project"]["repo"]
        if job.kind == "build":
            n = job.issue["number"]
            pr = self.board.pr_for_branch(branch)
            extra = (f"\nA PR already exists for this branch (#{pr['number']}). Read its review comments with "
                     f"`gh pr view {pr['number']} --repo {repo} --comments`, address every point, and push to the same branch. If the PR conflicts with main, first merge origin/main into "
                     f"the branch and resolve the conflicts, then rerun the tests. If its CI is red (`gh pr checks {pr['number']} --repo {repo}`), "
                     f"read the failing log (`gh run view <run id> --repo {repo} --log-failed`) and fix that first."
                     if pr and pr["state"] == "OPEN" else "")
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
                        f"Your sandbox also can't run Git Bash or render Godot windows, so run the tests by calling Godot "
                        f"directly: `\"$GODOT\" --headless --path game -s res://tests/run_tests.gd` (GODOT is "
                        f"D:/Dev/tools/godot/Godot_v4.7.2-stable_win64_console.exe). CI and the reviewer cover full runs and "
                        f"visual checks. If something in the sandbox is impossible, finish the rest and say what's left; your "
                        f"partial work is kept, not discarded.{extra}\n"
                        f"Finish with: a line `PR_TITLE: <short title>`, then `PR_BODY:` followed by each acceptance criterion and "
                        f"how you verified it, plus anything unverified. Make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")
            return (f"Implement GitHub issue #{n} in {repo}. You are already on branch `{branch}` in a fresh worktree "
                    f"based on origin/main. Follow your builder instructions and AGENTS.md. Push with "
                    f"`git push -u origin {branch}`. Prefix any issue or PR comment you write with `[agent:builder]`.{extra}\n"
                    f"Finish with a short report, and make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")
        if job.kind == "review":
            p = job.pr
            m = BRANCH.match(p["headRefName"])
            return (f"Review PR #{p['number']} in {repo} (it implements issue #{m.group(1) if m else '?'}). "
                    f"The PR branch is checked out here. Follow your reviewer instructions. Post exactly one comment "
                    f"with `gh pr comment {p['number']} --repo {repo} --body ...` that starts with `[agent:reviewer] REVIEW: APPROVE` "
                    f"or `[agent:reviewer] REVIEW: CHANGES`.")
        if job.kind == "audit":
            hours = (self.cfg.get("audit") or {}).get("every_hours", 1)
            room_py = Path(__file__).resolve().parent.parent / "room.py"
            return (f"Common Room audit for {repo}. {job.reason}.\n"
                    f"Start with `python \"{room_py}\" digest --hours {max(1, hours)}`, run from this directory: it lists "
                    f"runs, merges, failures, spend, the board and usage. Follow your auditor instructions. Write the owner's "
                    f"status report to `.room-report.md` in this directory; the room publishes it to the dashboard. Prefix "
                    f"every comment with `[agent:auditor]`. Any repo file change goes through a branch and PR. End with one "
                    f"line: `ACTIONS: [...]`, a JSON list of short strings naming each concrete change you made.")
        return (f"Common Room triggered a director run for {repo}. Milestone: {self.milestone}.\nTriggers: {job.reason}\n"
                f"Handle these triggers with your director instructions. Prefix every comment with `[agent:director]`. Any repo "
                f"file change must go through a branch and PR, never a push to main. End with one line: "
                f"`ACTIONS: [...]`, a JSON list of short strings naming each concrete board change you made (issues "
                f"created, closed or relabelled, PRs merged or bounced, owner tasks assigned). An empty list means you changed nothing.")

    # Worktrees -------------------------------------------------------------------------------------
    def prepare_worktree(self, job: Job, provider: str | None = None) -> tuple[Path, str | None]:
        base = self.root / ".claude" / "worktrees"
        base.mkdir(parents=True, exist_ok=True)
        # Codex's sandbox can only write inside its folder, and a worktree keeps its git metadata in the main
        # repo's .git, so Codex gets a self-contained clone (objects borrowed from the main repo via --reference).
        clone = provider == "codex"
        if job.kind == "build":
            n = job.issue["number"]
            branch = self.find_branch(n) or f"i{n}-{slug(job.issue['title'])}"
            start = f"origin/{branch}" if self.remote_head(branch) else "origin/main"
            path = base / (f"codex-{branch}" if clone else branch)
            self.checkout(path, start, branch, clone)
            return path, branch
        if job.kind == "review":
            branch = job.pr["headRefName"]
            path = base / f"{'codex-' if clone else ''}review-{job.pr['number']}"
            self.checkout(path, f"origin/{branch}", None, clone)
            return path, branch
        path = base / f"{'codex-' if clone else ''}{job.role}-{int(time.time())}"
        self.checkout(path, "origin/main", None, clone)
        return path, None

    def checkout(self, path: Path, start: str, branch: str | None, clone: bool):
        self.cleanup_worktree(path)
        if not clone:
            if branch:
                git(["worktree", "add", "-B", branch, str(path), start], self.root)
            else:
                git(["worktree", "add", "--detach", str(path), start], self.root)
            return
        url = git(["remote", "get-url", "origin"], self.root)
        git(["clone", "--quiet", "--reference", str(self.root), url, str(path)], self.root)
        git(["checkout", "--quiet", "-B", branch, start] if branch else ["checkout", "--quiet", "--detach", start], path)

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

    def cleanup_worktree(self, path: Path):
        if path.exists() and (path / ".git").is_dir():  # a self-contained clone (Codex jobs)
            import shutil, stat
            shutil.rmtree(path, onerror=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))
        elif path.exists():
            try:
                git(["worktree", "remove", "--force", str(path)], self.root)
            except RuntimeError:
                pass
        try:
            git(["worktree", "prune"], self.root)
        except RuntimeError:
            pass

    # Budget ----------------------------------------------------------------------------------------
    def weekly_used(self) -> float | None:
        row = self.ledger.latest_usage("claude").get("seven_day")
        return row["used_pct"] if row else None

    def stop_reason(self, provider: str) -> str | None:
        v = verdicts(self.ledger, self.cfg)[provider]
        # A worker already running may finish its window unless a reserve or breaker is hit.
        if not v.allowed and ("reserve" in v.reason or "breaker" in v.reason):
            return v.reason
        return None

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
            dead = run.get("pid") and run["pid"] != os.getpid() and not pid_alive(run["pid"])
            if not dead and now - run["started_at"] < ORPHAN_AGE:
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
                base = self.root / ".claude" / "worktrees"
                for path in [*base.glob(f"i{n}-*"), *base.glob(f"codex-i{n}-*")]:
                    branch = path.name.removeprefix("codex-")
                    if issue:
                        self.preserve_work(Job("build", run["role"], issue=issue), path, branch)
                    self.cleanup_worktree(path)
            if issue and "status:in-progress" in labels(issue):
                self.board.set_labels(n, add=["status:ready"], remove=["status:in-progress"])
                self.board.comment(n, "The worker was interrupted (the PC shut down or its dispatcher crashed). "
                                      "Back to ready; this does not count as an attempt.")
        return actions

    def run_once(self) -> dict:
        self.refresh_readings(time.time())
        for line in self.recover_orphans():
            self.log("   " + line)
        report = {"housekeeping": self.housekeeping() if self.only_provider in (None, "claude") and self.slot == 1 else []}
        for line in report["housekeeping"]:
            self.log("   " + line)
        now = time.time()
        trip = check_progress(self.ledger, self.project, now)
        if trip:
            self.ledger.trip(self.project, trip["kind"], trip["detail"], now=now)
            self.escalate_breaker(trip)
        allowed = verdicts(self.ledger, self.cfg, now, trip=True)
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

    def escalate_breaker(self, trip: dict):
        if self.dry_run:
            return
        title = f"[You] Room stopped: {trip['kind'].replace('_', ' ')}"
        existing = [i for i in self.board.open_issues(None) if i["title"] == title]
        if existing:
            return
        body = (f"Common Room stopped autonomous work.\n\n**Why:** {trip['detail']}\n\nDue: {time.strftime('%Y-%m-%d')}\n\n"
                "**What to do:** read the recent runs (`python D:/Dev/commonroom/room.py status`) and the run logs, then "
                "decide in an executive session what changes. Clear the stop with `python D:/Dev/commonroom/room.py breaker reset`.")
        board_mod.gh(["issue", "create", "--repo", self.cfg["project"]["repo"], "--title", title, "--body", body,
                      "--label", "owner:human", "--assignee", "kgaydhani"])


def _after(comment: dict, since: float) -> bool:
    created = comment.get("createdAt") or ""
    try:
        return calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) > since
    except ValueError:
        return False
