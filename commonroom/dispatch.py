"""Turn the board into work. Deterministic: decides what runs; models only do the work.

One pass (`run_once`):
1. Housekeeping with no model calls: merge approved green PRs, unblock issues whose dependencies closed.
2. Pick one piece of model work, highest value first: review a waiting PR, build a ready issue, or run the
   director on a trigger. Each needs an allowed provider for its role.
3. Run it, record usage and outputs, update the board, check breakers.
"""
from __future__ import annotations

import calendar
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


def owner_comments(issue: dict) -> list[dict]:
    return [c for c in issue.get("comments") or [] if not (c.get("body") or "").lstrip().startswith("[")]


# Policy ------------------------------------------------------------------------------------------
def verdicts(ledger: Ledger, cfg: dict, now: float | None = None) -> dict[str, gate.Verdict]:
    now = time.time() if now is None else now
    project = cfg["project"].get("name")
    runs = ledger.recent_runs(project, limit=cfg["breakers"]["no_output_runs"], autonomous=True, finished=True)
    breaker, trip = gate.breaker_verdict(ledger.open_breakers(project), runs, cfg["breakers"]["no_output_runs"])
    if trip:
        ledger.trip(project, trip["kind"], trip["detail"], now=now)
    result = {}
    for provider in ("claude", "codex"):
        v = gate.usage_verdict(provider, ledger.latest_usage(provider), config_mod.budget(cfg, provider), now,
                               cfg["freshness_seconds"])
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
    def __init__(self, cfg: dict, ledger: Ledger, board=None, log=print, dry_run: bool = False):
        self.cfg, self.ledger, self.log, self.dry_run = cfg, ledger, log, dry_run
        self.project = cfg["project"].get("name")
        self.root = Path(cfg["_root"]) if cfg.get("_root") else None
        self.board = board or GitHubBoard(cfg["project"]["repo"])
        self.milestone = cfg["goal"].get("milestone")
        self.roles = cfg.get("roles") or {}
        self.log_dir = Path(ledger.path).parent / "logs" / (self.project or "default")

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
            # Research reports (docs only) skip review: CI green is enough.
            if "review:skip" in labels(pr) and checks == "green":
                review = "APPROVE"
            if review == "APPROVE" and checks == "green":
                if not self.dry_run:
                    self.board.merge(pr["number"])
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
            done = subprocess.run([bash, "-lc", command], cwd=self.root, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=900)
            tail = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
            return [f"deploy {'ok' if done.returncode == 0 else 'FAILED'}: {tail[0][:160]}"]
        except Exception as exc:
            return [f"deploy FAILED: {type(exc).__name__}: {str(exc)[:160]}"]

    # Choosing work ---------------------------------------------------------------------------------
    def pick(self, allowed: dict[str, gate.Verdict]) -> tuple[Job, str, str | None] | None:
        builder_spec = self.roles.get("builder", ["claude:sonnet"])
        issue_labels = None
        for pr in self.board.open_prs():
            m = BRANCH.match(pr["headRefName"])
            if not m or "review:skip" in labels(pr):
                continue
            detail = self.board.pr_detail(pr["number"])
            if latest_review(detail) is None and checks_state(detail) != "red":
                if issue_labels is None:
                    issue_labels = {i["number"]: labels(i) for i in self.board.all_issues()}
                # Codex (the smaller subscription) is spent where independence matters most: high-risk changes.
                high_risk = "risk:high" in issue_labels.get(int(m.group(1)), set())
                spec = self.roles.get("reviewer_high_risk" if high_risk else "reviewer", builder_spec)
                built_by = self.last_provider(f"#{m.group(1)}", "builder")
                choice = choose_provider(spec, allowed, exclude=built_by) or choose_provider(spec, allowed)
                if choice:
                    return Job("review", "reviewer", pr=detail, reason=f"PR #{pr['number']} awaits {'high-risk ' if high_risk else ''}review"), *choice
        open_now = self.board.open_issues(None)
        ready = [i for i in open_now if {"status:ready", "role:builder"} <= labels(i) and "owner:human" not in labels(i)
                 and (not self.milestone or (i.get("milestone") or {}).get("title") == self.milestone)]
        scouting = [i for i in open_now if {"status:ready", "role:scout"} <= labels(i)]
        # The owner waiting on an answer outranks builds: a reply should never sit behind the whole queue.
        owner = self.owner_trigger()
        if owner:
            choice = choose_provider(self.roles.get("director", ["claude:opus"]), allowed)
            if choice:
                return Job("direct", "director", reason=owner), *choice
        scout_spec = self.roles.get("scout", ["claude:sonnet"])
        # Research must not starve behind an endless build queue: a ready scout task goes first every few hours.
        if scouting and time.time() - self.last_run_at("scout") > SCOUT_INTERLEAVE:
            choice = choose_provider(scout_spec, allowed)
            if choice:
                issue = min(scouting, key=lambda i: i["number"])
                return Job("build", "scout", issue=issue, reason=f"#{issue['number']} research is ready"), *choice
        for issue in sorted(ready, key=lambda i: i["number"]):
            choice = choose_provider(builder_spec, allowed)
            if choice:
                return Job("build", "builder", issue=issue, reason=f"#{issue['number']} is ready"), *choice
        for issue in sorted(scouting, key=lambda i: i["number"]):
            choice = choose_provider(scout_spec, allowed)
            if choice:
                return Job("build", "scout", issue=issue, reason=f"#{issue['number']} research is ready"), *choice
        sweep = self.capability_sweep_due()
        if sweep and choose_provider(scout_spec, allowed):
            issue = self.create_sweep_issue(sweep)
            if issue:
                return Job("build", "scout", issue=issue, reason=f"scheduled capability sweep: {sweep}"), *choose_provider(scout_spec, allowed)
        trigger = self.director_trigger()
        if trigger:
            choice = choose_provider(self.roles.get("director", ["claude:opus"]), allowed)
            if choice:
                return Job("direct", "director", reason=trigger), *choice
        return None

    def last_run_at(self, role: str) -> float:
        return next((r["started_at"] for r in self.ledger.recent_runs(self.project, limit=200) if r["role"] == role), 0.0)

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
            if names & {"owner:human", "kind:suggestion"} and last_is_owner and _after(comments[-1], since):
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
        git(["fetch", "--quiet", "origin"], self.root)
        worktree, branch = self.prepare_worktree(job)
        run_id, labelled = None, False
        try:
            before = self.remote_head(branch)
            weekly_start = self.weekly_used()
            run_id = self.ledger.start_run(self.project, task, job.role, provider, model or role.model)
            if job.issue:
                self.board.set_labels(job.issue["number"], add=["status:in-progress"], remove=["status:ready", "needs:changes"])
                labelled = True
            started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            result = run_worker(provider, role, self.prompt(job, branch), worktree, self.ledger, self.log_dir,
                                model=model, effort="high" if job.role == "director" else None,
                                stop_check=lambda: self.stop_reason(provider))
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
            self.cleanup_worktree(worktree)
        self.log(f"   {status}: {', '.join(o['ref'] for o in outputs) or 'no output'}  {note[:200]}")
        return {"status": status, "outputs": outputs, "note": note, "session": result.session_id}

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
        outputs = [{"kind": "director", "ref": str(a)[:120]} for a in actions]
        return ("done" if outputs else "failed"), outputs, tail

    def prompt(self, job: Job, branch: str | None) -> str:
        repo = self.cfg["project"]["repo"]
        if job.kind == "build":
            n = job.issue["number"]
            pr = self.board.pr_for_branch(branch)
            extra = (f"\nA PR already exists for this branch (#{pr['number']}). Read its review comments with "
                     f"`gh pr view {pr['number']} --repo {repo} --comments`, address every point, and push to the same branch."
                     if pr and pr["state"] == "OPEN" else "")
            if job.role == "scout":
                return (f"Do the research in GitHub issue #{n} in {repo}. You are on branch `{branch}` in a fresh worktree. "
                        f"Follow your scout instructions. Commit your report under docs/scouting/, push with "
                        f"`git push -u origin {branch}`, and open a PR whose body says `Closes #{n}` and summarises the findings. "
                        f"Open any `kind:suggestion` issues the deliverable asks for. Prefix comments with `[agent:scout]`.\n"
                        f"Finish with a short report, and make the last line exactly `STATUS: DONE` or `STATUS: BLOCKED`.")
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
        return (f"Common Room triggered a director run for {repo}. Milestone: {self.milestone}.\nTriggers: {job.reason}\n"
                f"Handle these triggers with your director instructions. Prefix every comment with `[agent:director]`. Any repo "
                f"file change must go through a branch and PR, never a push to main. End with one line: "
                f"`ACTIONS: [...]`, a JSON list of short strings naming each concrete board change you made (issues "
                f"created, closed or relabelled, PRs merged or bounced, owner tasks assigned). An empty list means you changed nothing.")

    # Worktrees -------------------------------------------------------------------------------------
    def prepare_worktree(self, job: Job) -> tuple[Path, str | None]:
        base = self.root / ".claude" / "worktrees"
        base.mkdir(parents=True, exist_ok=True)
        if job.kind == "build":
            n = job.issue["number"]
            branch = self.find_branch(n) or f"i{n}-{slug(job.issue['title'])}"
            path = base / branch
            self.cleanup_worktree(path)
            if self.remote_head(branch):
                git(["worktree", "add", "-B", branch, str(path), f"origin/{branch}"], self.root)
            else:
                git(["worktree", "add", "-B", branch, str(path), "origin/main"], self.root)
            return path, branch
        if job.kind == "review":
            branch = job.pr["headRefName"]
            path = base / f"review-{job.pr['number']}"
            self.cleanup_worktree(path)
            git(["worktree", "add", "--detach", str(path), f"origin/{branch}"], self.root)
            return path, branch
        path = base / f"director-{int(time.time())}"
        git(["worktree", "add", "--detach", str(path), "origin/main"], self.root)
        return path, None

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
        if path.exists():
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

    def run_once(self) -> dict:
        self.refresh_readings(time.time())
        report = {"housekeeping": self.housekeeping()}
        for line in report["housekeeping"]:
            self.log("   " + line)
        now = time.time()
        trip = check_progress(self.ledger, self.project, now)
        if trip:
            self.ledger.trip(self.project, trip["kind"], trip["detail"], now=now)
            self.escalate_breaker(trip)
        allowed = verdicts(self.ledger, self.cfg, now)
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
