"""Durable, single-session moonshots with active-time budgets and an external Gauntlet."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from commonroom import board, config
from commonroom.dispatch import git, pid_alive, verdicts
from commonroom.ledger import Ledger
from commonroom.workers import Role, run_worker, worker_tmp

DEFAULT_RULES = """Moonshot rules:
Work only on the assigned moonshot branch. Never push to main, open PRs, or merge.
Commit and push to your branch often. Reserve the final 20 active minutes for deliverables:
refresh the report, scorecard and project web export, record checks and remaining misses,
then commit and push. The director will harvest your branch later.
Write a scorecard JSON file beside the report, using this exact contract:
{"round": 2, "rows": [{"id": "formation", "bar": "shields in front at contact in >= 90% of seeds", "value": "94%", "met": true}]}
Use stable, unique row ids and measured values. Include every acceptance bar from the brief;
do not drop unmet rows. Use JSON booleans for met. Update it after every round.
"""
WRAP = "Wrap up now: refresh the report, scorecard and web export, commit and push."
HEADLESS = ("\n\nRun headless commands in the foreground. The harness will resume this same session as needed. "
            "Do not run room gate; the harness controls usage. Never open PRs, merge, or push to main.")
TERMINAL = {"done", "stopped", "failed"}


def safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Names must use letters, digits, underscores or hyphens (at most 80 characters)")
    return value


def parse_time(value: str | None, now: float | None = None) -> float | None:
    """Epoch, or the next local HH:MM (tomorrow if today's time has passed)."""
    if value is None:
        return None
    now = time.time() if now is None else now
    if ":" in value:
        clock = datetime.strptime(value, "%H:%M")
        when = datetime.fromtimestamp(now).replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
        if when.timestamp() < now:
            when += timedelta(days=1)
        return when.timestamp()
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("Time must be HH:MM or a positive epoch")
    return result


def active_time(used: float, began: float, ended: float, running: bool = True) -> float:
    """Monotonic timestamps count only worker execution, never waits or downtime."""
    return used + max(0, ended - began) if running else used


def remaining(job: dict) -> float:
    return max(0, job["budget_minutes"] * 60 - job["active_seconds_used"])


def guard_decision(windows: dict, policy: dict, model: str, now: float) -> tuple[str, str, float]:
    for window, cap in (("seven_day_" + model.lower(), policy.get("stop_model_weekly", 85)),
                        ("seven_day", policy.get("stop_weekly", 85))):
        row = windows.get(window) or {}
        if row.get("resets_at") and row["resets_at"] <= now:
            continue
        if row.get("used_pct", 0) > cap:
            return "stop", f"{window} above {cap}%", 0
    row = windows.get("five_hour") or {}
    if row.get("used_pct", 0) >= policy.get("pause_5h", 80) and row.get("resets_at", 0) and row["resets_at"] > now:
        return "pause", "5-hour window hot", row["resets_at"] + 120
    return "go", "", 0


def recovery_decision(job: dict, alive: bool, now: float) -> str | None:
    if job["status"] in ("running", "paused") and not alive:
        return "recover"
    if job["status"] == "queued" and job["start_after"] <= now:
        return "launch"
    return None


def parse_scorecard(text: str | None) -> dict | None:
    try:
        card = json.loads(text or "")
        if not isinstance(card, dict) or type(card.get("round")) is not int or card["round"] < 0:
            return None
        rows = card.get("rows")
        if not isinstance(rows, list) or not rows:
            return None
        ids = set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                    or row["id"] in ids or not isinstance(row.get("bar"), str)
                    or not isinstance(row.get("value"), str) or type(row.get("met")) is not bool):
                return None
            ids.add(row["id"])
        return card
    except (ValueError, TypeError):
        return None


def merge_scorecards(card: dict | None, verified: dict | None, previous: dict | None = None) -> dict | None:
    """Verifier rows win by id. Retain disappeared bars as unmet rather than silently passing."""
    if card is None and verified is None:
        return None
    rows = {r["id"]: dict(r) for r in (card or {}).get("rows", [])}
    rows.update({r["id"]: dict(r) for r in (verified or {}).get("rows", [])})
    for row in (previous or {}).get("rows", []):
        if row["id"] not in rows:
            rows[row["id"]] = dict(row, met=False, value="missing from current scorecard")
    return {"round": (card or verified)["round"], "rows": list(rows.values())}


def score_summary(card: dict | None) -> tuple[int, int]:
    rows = (card or {}).get("rows", [])
    return sum(r["met"] for r in rows), len(rows)


def next_prompt(card: dict | None, previous: dict | None, round_number: int, max_rounds: int,
                seconds_left: float, report: str) -> str | None:
    """Normal completion: stop at the bar/budget/round limit, otherwise identify each miss."""
    if round_number >= max_rounds or seconds_left <= 25 * 60:
        return None
    if card and all(r["met"] for r in card["rows"]):
        return None
    if card is None:
        return (f"Next round (Gauntlet): the scorecard is missing or unparseable. Write valid scorecard JSON at "
                f"{scorecard_path(report)}, measure every bar in the brief, and attack unmet rows. "
                f"{seconds_left / 60:.1f} active minutes remain; keep the last 20 for deliverables.")
    met_before = {r["id"] for r in (previous or {}).get("rows", []) if r["met"]}
    misses = [f"- {r['id']}: bar={r['bar']}; value={r['value']}" +
              (" (REGRESSED: previously met, now unmet)" if r["id"] in met_before else "")
              for r in card["rows"] if not r["met"]]
    return (f"Next round (Gauntlet), round {round_number + 1}: fix and re-measure every unmet bar:\n" +
            "\n".join(misses) + f"\n{seconds_left / 60:.1f} active minutes remain; keep the last 20 for deliverables. "
            f"Refresh {report} and {scorecard_path(report)}. Commit and push.")


def continuation(job: dict, reason: str) -> str:
    return (f"Continue exactly where you stopped; {remaining(job) / 60:.1f} active minutes remain. {reason}. "
            "Keep the last 20 for deliverables. Commit and push as you go." +
            ("\n" + WRAP if job["wrap_sent"] else ""))


def recovery_prompt(job: dict) -> str:
    # An outgoing prompt is persisted before a worker starts, including feedback consumed for it.
    prompt = job["next_prompt"] or ""
    if not job["session_id"]:
        prompt = Path(job["brief_path"]).read_text(encoding="utf-8") + "\n\n" + prompt
    return prompt + "\n\n" + continuation(job, "The PC restarted; continue exactly where you stopped")


def scorecard_path(report: str) -> str:
    return Path(report).with_suffix(".scorecard.json").as_posix()


def add(cfg: dict, ledger: Ledger, name: str, brief: Path, **options) -> dict:
    name = safe_name(name)
    project = safe_name(cfg["project"].get("name") or "default")
    root = Path(cfg["_root"]).resolve()
    if ledger.moonshot(project, name):
        raise ValueError(f"Moonshot {name} already exists; use resume")
    policy = config.merge(config.DEFAULTS["moonshot"], cfg.get("moonshot") or {})
    for key in ("minutes", "model", "effort", "base", "max_rounds"):
        if options.get(key) is not None:
            policy[key] = options[key]
    if not math.isfinite(policy["minutes"]) or policy["minutes"] <= 0 or policy["max_rounds"] < 1:
        raise ValueError("Minutes and max rounds must be positive")
    date = datetime.now().strftime("%Y-%m-%d")
    report = options.get("report") or f"docs/moonshot/{date}-{name}.md"
    if Path(report).is_absolute() or ".." in Path(report).parts:
        raise ValueError("Report must be a relative path inside the worktree")
    rules = root / ".claude" / "moonshot_rules.md"
    text = brief.read_text(encoding="utf-8") + "\n\n" + (rules.read_text(encoding="utf-8") if rules.is_file() else DEFAULT_RULES)
    text += f"\nReport: {report}\nScorecard: {scorecard_path(report)}\nBranch: moonshot/{name}-{date}\n"
    start = parse_time(options.get("start")) or 0
    until = parse_time(options.get("until"))
    folder = ledger.path.parent / "moonshots" / project / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "brief.md"
    path.write_text(text, encoding="utf-8")
    ledger.add_moonshot(project, name, brief_path=str(path), branch=f"moonshot/{name}-{date}",
                        worktree=str(root / ".claude" / "worktrees" / f"ms-{name}"), report=report,
                        model=policy["model"], effort=policy["effort"], budget_minutes=policy["minutes"],
                        max_rounds=policy["max_rounds"], base=policy["base"], start_after=start,
                        until=until, verify_cmd=options.get("verify_cmd"))
    return ledger.moonshot(project, name)


def stop(ledger: Ledger, project: str, name: str):
    job = require_job(ledger, project, name)
    fields = {"stop_requested": 1}
    if job["status"] == "queued":
        fields.update(status="stopped", finished_at=time.time(), detail="owner requested stop")
    ledger.update_moonshot(project, name, **fields)


def resume(ledger: Ledger, project: str, name: str, extend: float = 0, prompt: str | None = None):
    job = require_job(ledger, project, name)
    if job["status"] not in TERMINAL or (job["runner_pid"] and pid_alive(job["runner_pid"])):
        raise ValueError("Only a stopped or finished moonshot whose runner has exited can resume")
    if not math.isfinite(extend) or extend < 0:
        raise ValueError("Extension must be non-negative")
    updated = dict(job, budget_minutes=job["budget_minutes"] + extend)
    if remaining(updated) <= 0:
        raise ValueError("Budget is used; provide --extend MIN")
    ledger.update_moonshot(project, name, status="queued", budget_minutes=updated["budget_minutes"],
                           runner_pid=None, stop_requested=0, wait_until=0, until=None, start_after=0,
                           max_rounds=job["round"] + job["max_rounds"], wrap_sent=0, finished_at=None,
                           issue_url=None, next_prompt=prompt or continuation(updated, "The owner resumed this moonshot"))


def require_job(ledger: Ledger, project: str, name: str) -> dict:
    job = ledger.moonshot(project, name)
    if job is None:
        raise ValueError(f"Unknown moonshot: {name}")
    return job


def overview(ledger: Ledger, project: str) -> list[dict]:
    result = []
    for job in ledger.moonshots(project):
        met, total = score_summary(parse_scorecard(job["last_scorecard"]))
        result.append(dict(job, minutes_used=round(job["active_seconds_used"] / 60, 1),
                           minutes_left=round(remaining(job) / 60, 1), met=met, total=total,
                           cost_usd=ledger.moonshot_cost(project, job["name"])))
    return result


def log_path(ledger: Ledger, project: str, name: str) -> Path:
    folder = ledger.path.parent / "logs" / "moonshots"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{safe_name(project)}-{safe_name(name)}.log"


def launch(cfg: dict, ledger: Ledger, job: dict) -> int:
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / "room.py"),
               "moonshot", "run", job["name"], "--ledger", str(ledger.path.resolve())]
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with log_path(ledger, job["project"], job["name"]).open("a", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=cfg["_root"], stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                                   start_new_session=os.name != "nt")
    return process.pid


def schedule(cfg: dict, ledger: Ledger, allowed: bool | None = None, now: float | None = None,
             alive=pid_alive, spawn=launch) -> list[str]:
    """Atomic capacity/launch claim across heartbeat dispatchers. No worker inference here."""
    project = cfg["project"].get("name") or "default"
    if not ledger.moonshots(project):
        return []
    now = time.time() if now is None else now
    if allowed is None:
        allowed = verdicts(ledger, cfg, now)["claude"].allowed
    policy = cfg.get("moonshot") or config.DEFAULTS["moonshot"]
    actions = []
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        jobs = ledger.moonshots(project)
        live = {j["name"] for j in jobs if j["runner_pid"] and alive(j["runner_pid"])}
        slots = max(0, policy.get("max_parallel", 3) - len(live))
        for job in sorted(jobs, key=lambda j: j["status"] not in ("running", "paused")):
            decision = recovery_decision(job, job["name"] in live, now)
            if not decision or not slots:
                continue
            if decision == "launch" and not allowed:
                continue
            if decision == "recover":
                prompt = recovery_prompt(job)
                ledger.db.execute("UPDATE moonshots SET next_prompt=? WHERE project=? AND name=?", (prompt, project, job["name"]))
            pid = spawn(cfg, ledger, job)
            ledger.db.execute("UPDATE moonshots SET status='running', runner_pid=?, updated_at=? WHERE project=? AND name=?",
                              (pid, now, project, job["name"]))
            slots -= 1
            actions.append(f"moonshot {job['name']}: {decision} (pid {pid})")
    for job in ledger.moonshots(project):
        if (job["status"] in TERMINAL and not job["issue_url"] and job["finished_at"]
                and not (job["runner_pid"] and alive(job["runner_pid"]))):
            try:
                notify(cfg, ledger, job)
            except Exception as exc:
                actions.append(f"moonshot {job['name']}: harvest notification pending: {exc}")
    return actions


def prepare(cfg: dict, job: dict):
    tree = Path(job["worktree"])
    if not tree.exists():
        root = Path(cfg["_root"])
        git(["fetch", "--quiet", "origin"], root)
        try:
            git(["rev-parse", "--verify", job["branch"]], root)
            base = job["branch"]
        except RuntimeError:
            base = None
        args = ["worktree", "add", str(tree), base] if base else ["worktree", "add", "-b", job["branch"], str(tree), job["base"]]
        tree.parent.mkdir(parents=True, exist_ok=True)
        git(args, root)
    if git(["branch", "--show-current"], tree) != job["branch"]:
        raise RuntimeError("Moonshot worktree is on the wrong branch")


def save(job: dict):
    tree = Path(job["worktree"])
    if git(["branch", "--show-current"], tree) != job["branch"] or not job["branch"].startswith("moonshot/"):
        raise RuntimeError("Refusing to save outside the assigned moonshot branch")
    if git(["status", "--porcelain"], tree):
        git(["add", "-A"], tree)
        git(["commit", "-q", "-m", f"Moonshot {job['name']}: work at the end of a session segment"], tree)
    git(["push", "-q", "-u", "origin", job["branch"]], tree)


def read_scorecard(job: dict, previous: dict | None, log=print) -> dict | None:
    path = Path(job["worktree"]) / scorecard_path(job["report"])
    try:
        card = parse_scorecard(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        card = None
    verified = None
    if job["verify_cmd"]:
        try:
            done = subprocess.run(job["verify_cmd"], shell=True, cwd=job["worktree"], capture_output=True,
                                  text=True, encoding="utf-8", timeout=300,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            verified = parse_scorecard(done.stdout)
            if done.returncode or verified is None:
                log(f"Verifier failed (exit {done.returncode}) or returned invalid JSON; Gauntlet needs a fresh scorecard")
                return None
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            log(f"Verifier failed: {exc}")
            return None
    return merge_scorecards(card, verified, previous)


def notify(cfg: dict, ledger: Ledger, job: dict):
    """Retryable harvest issue, with a durable marker to find a creation interrupted by a crash."""
    repo = cfg["project"].get("repo")
    if not repo:
        raise ValueError("project.repo is required for harvest notifications")
    card = parse_scorecard(job["last_scorecard"])
    met, total = score_summary(card)
    title = f"Moonshot {job['name']} finished: {met}/{total} bar rows met"
    marker = f"commonroom-moonshot:{job['name']}:{job['finished_at']}"
    existing = board.gh(["issue", "list", "--repo", repo, "--state", "all", "--search", f'"{marker}" in:body',
                         "--json", "url,body"]) or []
    url = next((i["url"] for i in existing if marker in i["body"]), None)
    if not url:
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ")
        rows = [f"| {cell(r['id'])} | {cell(r['bar'])} | {cell(r['value'])} | {'yes' if r['met'] else 'no'} |"
                for r in (card or {}).get("rows", [])]
        body = (f"Branch: `{job['branch']}`\nReport: `{job['report']}`\nScorecard: `{scorecard_path(job['report'])}`\n\n"
                f"State: {job['status']}; {job['detail'] or ''}\nActive minutes: {job['active_seconds_used'] / 60:.1f}\n"
                f"Cost (API equivalent): ${ledger.moonshot_cost(job['project'], job['name']):.2f}\n\n"
                "| Id | Bar | Value | Met |\n| --- | --- | --- | --- |\n" + "\n".join(rows) +
                ("\nScorecard missing or invalid." if not card else "") +
                f"\n\nDirector: harvest this branch. No PR was opened or merged.\n\n<!-- {marker} -->")
        body_path = Path(job["brief_path"]).parent / "harvest.md"
        body_path.write_text(body, encoding="utf-8")
        args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", str(body_path), "--label", "needs:director"]
        if cfg.get("goal", {}).get("milestone"):
            args += ["--milestone", cfg["goal"]["milestone"]]
        url = board.gh(args)
    ledger.update_moonshot(job["project"], job["name"], issue_url=str(url))


def run(cfg: dict, ledger: Ledger, name: str, until: float | None = None) -> int:
    project = cfg["project"].get("name") or "default"
    job = require_job(ledger, project, name)
    if job["status"] in TERMINAL:
        return 0
    # Hand-run entry point also respects the gate and scheduled start. Detached starts are already gated.
    if job["status"] == "queued" and (job["start_after"] > time.time() or not verdicts(ledger, cfg)["claude"].allowed):
        return 0
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        job = require_job(ledger, project, name)
        if job["runner_pid"] and job["runner_pid"] != os.getpid() and pid_alive(job["runner_pid"]):
            return 0
        live = sum(j["name"] != name and bool(j["runner_pid"] and pid_alive(j["runner_pid"])) for j in ledger.moonshots(project))
        if live >= (cfg.get("moonshot") or {}).get("max_parallel", 3):
            return 0
        recovered = job["status"] in ("running", "paused") and job["runner_pid"] != os.getpid()
        prompt = recovery_prompt(job) if recovered else job["next_prompt"]
        ledger.db.execute("UPDATE moonshots SET runner_pid=?, status='running', next_prompt=? WHERE project=? AND name=?",
                          (os.getpid(), prompt, project, name))
    if until is not None:
        ledger.update_moonshot(project, name, until=until)
    path = log_path(ledger, project, name)
    def log(message):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    policy = cfg.get("moonshot") or config.DEFAULTS["moonshot"]
    run_id = None
    status, detail = "done", "budget used"
    assessed, final_card = False, None
    try:
        # Close only this job's abandoned segments; carry over its last active-time checkpoint.
        for old in ledger.running_runs(project):
            if old["role"] == "moonshot" and old["task"] == f"moonshot:{name}":
                ledger.close_orphan(old["id"], "PC restarted; session resumed", job["checkpoint_at"] or old["started_at"])
        prepare(cfg, job)
        while True:
            job = require_job(ledger, project, name)
            now = time.time()
            action, reason, reset = guard_decision(ledger.latest_usage("claude"), policy, job["model"], now)
            if job["stop_requested"] or action == "stop":
                status, detail = "stopped", reason or "owner requested stop"
                break
            if remaining(job) <= 0 or (job["until"] and now >= job["until"]):
                detail = "hard wall-clock deadline reached" if job["until"] and now >= job["until"] else "active budget used"
                break
            wait = max(job["wait_until"], reset)
            if wait > now:
                if job["status"] != "paused" or job["wait_until"] != wait:
                    ledger.update_moonshot(project, name, status="paused", wait_until=wait)
                    log(f"paused until {datetime.fromtimestamp(wait):%Y-%m-%d %H:%M:%S}; active budget retained")
                time.sleep(min(5, wait - now))
                continue
            prompt = job["next_prompt"] or (Path(job["brief_path"]).read_text(encoding="utf-8") if not job["session_id"]
                                            else continuation(job, "Resume the session"))
            wrap = remaining(job) <= 20 * 60 and not job["wrap_sent"]
            if wrap:
                prompt += "\n\n" + WRAP
            session = job["session_id"] or str(uuid.uuid4())
            # Keep the exact outgoing prompt durable so recovery cannot lose consumed feedback.
            prompt = ledger.prepare_moonshot_prompt(project, name, prompt, session, wrap)
            run_id = ledger.start_run(project, f"moonshot:{name}", "moonshot", "claude", job["model"],
                                      effort=job["effort"], session_id=session)
            began = time.monotonic()
            used = job["active_seconds_used"]
            last_checkpoint = began
            def checkpoint(force=False):
                nonlocal last_checkpoint
                stamp = time.monotonic()
                if force or stamp - last_checkpoint >= 5:
                    ledger.update_moonshot(project, name, active_seconds_used=active_time(used, began, stamp), checkpoint_at=time.time())
                    last_checkpoint = stamp
                return stamp
            def check():
                stamp = checkpoint()
                current = require_job(ledger, project, name)
                if current["stop_requested"]:
                    return "owner requested stop"
                action, reason, _ = guard_decision(ledger.latest_usage("claude"), policy, job["model"], time.time())
                if action != "go":
                    return reason
                left = job["budget_minutes"] * 60 - active_time(used, began, stamp)
                if left <= 0:
                    return "active budget used"
                if current["until"] and time.time() >= current["until"]:
                    return "hard wall-clock deadline reached"
                if left <= 20 * 60 and not current["wrap_sent"]:
                    return "deliverables due"
                return None
            log(f"segment start: round {job['round'] + 1}, {remaining(job) / 60:.1f} active minutes left, session {session}")
            assessed = False
            try:
                result = run_worker("claude", Role("moonshot", job["model"], [],
                                    "Work autonomously on the assigned moonshot brief. " + DEFAULT_RULES),
                                    prompt + HEADLESS, Path(job["worktree"]), ledger,
                                    path.parent / f"{project}-{name}", model=job["model"], effort=job["effort"],
                                    timeout=remaining(job) + 30, stop_check=check, resume_session=session if job["session_id"] else None,
                                    new_session_id=session)
            finally:
                checkpoint(force=True)
            segment_status = "done" if result.ok else "limited" if result.limited_until else "stopped" if result.interrupted else "failed"
            session = result.session_id or session
            ledger.finish_run(run_id, segment_status, [{"kind": "branch", "ref": job["branch"]}], result.input_tokens,
                              result.output_tokens, result.cost_usd, (result.final_text or "")[-800:], session_id=session)
            run_id = None
            ledger.update_moonshot(project, name, session_id=session)
            save(job)
            job = require_job(ledger, project, name)
            log(f"segment ended: {segment_status}; {remaining(job) / 60:.1f} active minutes left")
            action, reason, _ = guard_decision(ledger.latest_usage("claude"), policy, job["model"], time.time())
            if job["stop_requested"] or action == "stop":
                status, detail = "stopped", reason or "owner requested stop"
                break
            previous = parse_scorecard(job["last_scorecard"])
            if result.ok:
                card = read_scorecard(job, previous, log)
                assessed, final_card = True, card
                rounds = job["round"] + 1
                prompt = next_prompt(card, previous, rounds, job["max_rounds"], remaining(job), job["report"])
                ledger.update_moonshot(project, name, round=rounds, last_scorecard=json.dumps(card) if card else job["last_scorecard"],
                                       next_prompt=prompt)
                if prompt is None:
                    detail = "bar met" if card and all(r["met"] for r in card["rows"]) else "round or remaining budget limit reached"
                    break
            elif result.limited_until:
                ledger.update_moonshot(project, name, status="paused", wait_until=result.limited_until + 90,
                                       next_prompt=continuation(job, "A provider usage limit interrupted you"))
            elif result.interrupted == "deliverables due":
                ledger.update_moonshot(project, name, wrap_sent=1, next_prompt=WRAP)
            elif result.interrupted:
                ledger.update_moonshot(project, name, next_prompt=continuation(job, "You were paused to leave usage for the owner"))
            else:
                status, detail = "failed", "; ".join(result.errors)[-800:] or "worker failed"
                break
        # Refresh the final scorecard on interrupted/guard stops too.
        job = require_job(ledger, project, name)
        card = final_card if assessed else read_scorecard(job, parse_scorecard(job["last_scorecard"]), log)
        ledger.update_moonshot(project, name, last_scorecard=json.dumps(card) if card else None)
        save(job)
    except Exception as exc:
        status, detail = "failed", f"{type(exc).__name__}: {exc}"
        log(detail)
        if run_id:
            ledger.finish_run(run_id, "room-error", note=detail, session_id=job["session_id"])
        try:
            save(job)
        except Exception as save_exc:
            log(f"Save failed; worktree retained: {save_exc}")
    finally:
        # Worktree is retained for harvest and resumption. Only disposable worker TEMP is removed.
        shutil.rmtree(worker_tmp(job["worktree"]), ignore_errors=True)
        ledger.update_moonshot(project, name, status=status, detail=detail, finished_at=time.time())
    try:
        notify(cfg, ledger, require_job(ledger, project, name))
    except Exception as exc:
        log(f"Harvest notification pending (heartbeat will retry): {exc}")
    finally:
        ledger.update_moonshot(project, name, runner_pid=None)
    return 1 if status == "failed" else 0
