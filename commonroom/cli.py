"""`room` command line: usage, gate, status and the status-line tap."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from commonroom import beat as beat_mod, board, config as config_mod, gate, guard, metrics, policy as policy_mod, registry, usage
from commonroom import away, owner_reserve, pie_notice, progress as progress_mod, raises, sessions, standings, weekly
from commonroom.dispatch import READER_COOLDOWN, Dispatcher, pie_holds, runnable_roles, stale_claims, verdicts, with_pie
from commonroom import ledger as ledger_mod
from commonroom import handshake, hook
from commonroom.ledger import Ledger
from commonroom.lock import acquire_lock, claim_lock, release_lock, suite_lock

PROVIDERS = ("claude", "codex")


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def _clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %H:%M") if ts else "?"


def _project_name(cfg: dict) -> str | None:
    return cfg["project"].get("name")


# Commands ---------------------------------------------------------------------
def cmd_guard_command(args) -> int:
    """Claude Code PreToolUse hook for workers: exit 2 blocks a filter-based process kill (commonroom#18)."""
    return guard.main()


def cmd_tap(args) -> int:
    """Status-line command: record Claude's rate limits, print a compact line. Must never fail loudly."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    parts = []
    try:
        model = (data.get("model") or {}).get("display_name")
        if model:
            parts.append(model)
        ctx = (data.get("context_window") or {}).get("used_percentage")
        if ctx is not None:
            parts.append(f"ctx {ctx:.0f}%")
        observations = usage.from_statusline(data)
        if observations:
            ledger = Ledger()
            ledger.record_usage(observations)
            folder = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
            cfg = config_mod.load(config_mod.find_config(Path(folder)) if folder else None)
            if owner_reserve.for_project(ledger, cfg):
                v = _verdicts(ledger, cfg, time.time())["claude"]   # a registered project: the verdict its dispatcher acts on, pie holds included (#272)
            else:
                readings = ledger.latest_usage("claude")
                v = gate.usage_verdict("claude", readings, config_mod.budget(cfg, "claude"), time.time(), cfg["freshness_seconds"])
            ledger.close()
            five = next((o for o in observations if o["window"] == "five_hour"), None)
            week = next((o for o in observations if o["window"] == "seven_day"), None)
            if five:
                parts.append(f"5h {five['used_pct']:.0f}% (resets {_clock(five['resets_at'])[4:]})")
            if week:
                pace = v.detail.get("weekly_pace")
                parts.append(f"week {week['used_pct']:.0f}%" + (f" / pace {pace:.0f}%" if pace is not None else ""))
            parts.append("room: go" if v.allowed else "room: hold")
    except Exception as exc:  # the status line must keep rendering
        parts.append(f"room tap error: {type(exc).__name__}")
    parts += _tap_over_share(data)
    print(" | ".join(parts) or "room")
    return 0


def _tap_over_share(data: dict) -> list[str]:
    """["<project> over its share (est.)"] when the folder this status line runs in is a registered project that is over
    its line (commonroom#243). Nothing for any other folder, and nothing when the registry or ledger can't be read."""
    try:
        folder = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
        if not folder:
            return []
        reg = registry.load()
        key = next((k for k, p in reg.projects.items() if sessions.path_key(folder) == sessions.path_key(p.path)
                    or sessions.path_key(folder).startswith(sessions.path_key(p.path).rstrip(os.sep) + os.sep)), None)
        if key is None:
            return []
        ledger = Ledger()
        try:
            found = []
            for provider in standings.SESSION_PROVIDERS:
                report = standings.standings(ledger, reg, provider, time.time())
                row = report and pie_notice.over(report, key)
                if row:
                    found.append(f"{key} over its share on {provider}: est. {row.spent:.0f}% of {row.allowed:.0f}% "
                                 f"(room pie)")
            return found
        finally:
            ledger.close()
    except Exception:
        return []


def _verdicts(ledger, cfg: dict, now: float) -> dict:
    """The gate's verdicts with the pie's holds on top (commonroom#240), so its reason shows when it is what holds.
    Read-only, as `verdicts` is for views."""
    v = verdicts(ledger, cfg, now)
    return with_pie(v, pie_holds(ledger, cfg, v, now) or {})


def cmd_usage(args) -> int:
    ledger, cfg, now = Ledger(), config_mod.load(), time.time()
    v = _verdicts(ledger, cfg, now)
    out = {}
    for provider in PROVIDERS:
        readings = ledger.latest_usage(provider)
        out[provider] = {"verdict": vars(v[provider]), "windows": readings}
        if args.json:
            continue
        print(f"{provider}: {'GO' if v[provider].allowed else 'HOLD'} - {v[provider].reason}")
        if not readings:
            print("  no readings yet")
        for window, r in sorted(readings.items()):
            resets = f"resets {_clock(r['resets_at'])} (in {_ago(r['resets_at'] - now)})" if r["resets_at"] else ""
            print(f"  {window:<22} {r['used_pct']:5.1f}% used  {resets}  [{r['source']}, {_ago(now - r['observed_at'])} ago]")
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    ledger.close()
    return 0


def cmd_gate(args) -> int:
    if os.environ.get("COMMONROOM_WORKER"):
        # The dispatcher gated this job's start; a hold on *new* work must not make a running worker stop itself.
        print("GO room worker: the dispatcher already checked the budget for this job")
        return 0
    ledger = Ledger()
    v = verdicts(ledger, config_mod.load())[args.provider]
    ledger.close()
    print(("GO " if v.allowed else "HOLD ") + v.reason)
    return 0 if v.allowed else 1


def cmd_beat(args) -> int:
    """One heartbeat over every registered project (commonroom#247)."""
    return beat_mod.beat(ledger_mod.default_path().parent, log=lambda line: print(f"{datetime.now():%m-%d %H:%M:%S} {line}", flush=True))


def cmd_probe(args) -> int:
    ledger, failures = Ledger(), 0
    targets = PROVIDERS if args.provider == "all" else (args.provider,)
    for provider in targets:
        # The same cooldown as the dispatchers' refresh, so a failing reader is launched once per 30 minutes in all.
        if not ledger.reader_claim(provider, time.time(), READER_COOLDOWN):
            print(f"{provider}: skipped - a reader ran within the last {READER_COOLDOWN // 60} minutes")
            continue
        try:
            observations = usage.codex_limits() if provider == "codex" else usage.claude_screen_limits()
            ledger.record_usage(observations)
            ledger.reader_clear(provider)
            print(f"{provider}: " + ", ".join(f"{o['window']} {o['used_pct']:.0f}%" for o in observations))
        except Exception as exc:
            failures += 1
            print(f"{provider}: probe failed - {exc}")
    ledger.close()
    return 1 if failures == len(targets) else 0


def cmd_status(args) -> int:
    cfg, ledger, now = config_mod.load(), Ledger(), time.time()
    project = cfg["project"]
    print(f"Project: {project.get('name') or '(no room.toml found)'}  {project.get('repo') or ''}")
    if project.get("mission"):
        print(f"Mission: {project['mission']}")
    repo, goal = project.get("repo"), cfg["goal"].get("milestone")
    if repo:
        try:
            if goal:
                m = board.milestone(repo, goal)
                if m:
                    total = m["open_issues"] + m["closed_issues"]
                    due = f", due {m['due_on'][:10]}" if m.get("due_on") else ""
                    print(f"Goal: {goal} - {m['closed_issues']}/{total} done{due}")
                else:
                    print(f"Goal: {goal} - milestone not found on the board")
            owner = board.issues(repo, label=board.OWNER_LABEL)
            print(f"Your queue ({len(owner)}):")
            for i in sorted(owner, key=lambda i: board.due_date(i["body"]) or "9999"):
                print(f"  #{i['number']} {i['title']}  [due {board.due_date(i['body']) or '-'}]")
            ready = board.issues(repo, label=board.READY_LABEL)
            print(f"Ready for agents: {len(ready)} (cap {cfg['board']['ready_cap']})")
        except Exception as exc:
            print(f"Board unavailable: {exc}")
    for provider, v in _verdicts(ledger, cfg, now).items():
        print(f"{provider}: {'GO' if v.allowed else 'HOLD'} - {v.reason}")
    runs = ledger.recent_runs(project.get("name"), limit=5)
    if runs:
        print("Recent runs:")
        for r in runs:
            outputs = ", ".join(o.get("ref", o.get("kind", "?")) for o in r["outputs"]) or "no output"
            print(f"  {_clock(r['started_at'])} {r['role']}/{r['provider']} {r['task'] or '-'} {r['status']} -> {outputs}")
    answer = progress_mod.landed(cfg, now - 86400)
    if progress_mod.declared(cfg):
        print(f"Landed in 24h: {answer.count if answer.count is not None else 'no answer (' + answer.why + '); merges count'}")
        for line in answer.lines[-3:]:
            print(f"  {line}")
    for b in ledger.open_breakers(project.get("name")):
        print(f"BREAKER {b['kind']}: {b['detail']}  (clear with: room breaker reset)")
    ledger.close()
    return 0


def cmd_run(args) -> int:
    cfg = config_mod.load()
    if not cfg.get("_root"):
        print("No room.toml found; run inside a project")
        return 2
    ledger = Ledger()
    name = cfg['project'].get('name') or 'default'
    slot = getattr(args, "slot", 1) or 1
    suffix = (f"-{args.provider}" if args.provider else "") + (f"-{slot}" if slot > 1 else "")
    tag = f" [{suffix[1:]}]" if suffix else ""
    # One dispatcher per provider and slot (or one for all): each has its own lock, so they work in parallel.
    lock = ledger.path.parent / f"{name}{suffix}.lock"
    if not args.dry_run and not acquire_lock(lock):
        print(f"another dispatcher holds {lock}; exiting")
        ledger.close()
        return 0
    dispatcher = Dispatcher(cfg, ledger, dry_run=args.dry_run, only_provider=args.provider, slot=slot,
                            log=lambda line: print(f"{datetime.now():%H:%M:%S}{tag} {line}", flush=True))
    # `room stop` writes a timestamp; every dispatcher started before it exits between jobs.
    stop_file = ledger.path.parent / f"{name}.stop"
    started = time.time()
    try:
        while True:
            try:
                report = dispatcher.run_once()
            except Exception as exc:  # one bad pass (GitHub hiccup, git race) must not stop the room
                import traceback
                print(f"{datetime.now():%H:%M:%S}{tag} pass failed, continuing: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
                traceback.print_exc()
                if stop_file.exists() and stop_file.stat().st_mtime > started:
                    print(f"{datetime.now():%H:%M:%S}{tag} stop requested; exiting", flush=True)
                    return 0
                time.sleep(30)
                continue
            if stop_file.exists() and stop_file.stat().st_mtime > started:
                print(f"{datetime.now():%H:%M:%S}{tag} stop requested; exiting between jobs", flush=True)
                return 0
            idle = report.get("job") is None
            if args.dry_run or (not args.loop and not args.until_idle) or (args.until_idle and idle):
                return 0
            # Idle passes are cheap (no model calls); wait longer when nothing could run.
            time.sleep(args.interval if idle else 5)
    except KeyboardInterrupt:
        return 0
    finally:
        ledger.close()
        if not args.dry_run:
            lock.unlink(missing_ok=True)


def desktop_window(label: str) -> str | None:
    label = label.lower()
    if "5-hour" in label or "session" in label:
        return "five_hour"
    if label.startswith("weekly"):
        return "seven_day" if "all models" in label else "seven_day_" + label.split()[-1]
    return None


def cmd_housekeep(args) -> int:
    """Housekeeping on its own, between and during jobs: merges, unblocking, stale claims. Never picks or runs a job."""
    cfg = config_mod.load()
    if not cfg.get("_root"):
        print("No room.toml found; run inside a project")
        return 2
    ledger = Ledger()
    dispatcher = Dispatcher(cfg, ledger, log=lambda line: print(f"{datetime.now():%H:%M:%S} [housekeep] {line}", flush=True))
    try:
        if dispatcher.housekeeping_locked():
            print(f"another housekeeping pass holds {dispatcher.housekeeping_lock}; exiting")
            return 0
        housekeep_loop(dispatcher, args.minutes, args.every)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        ledger.close()


def housekeep_loop(dispatcher, minutes: float, every: float, clock=time.time, sleep=time.sleep) -> int:
    """Run a housekeeping pass now, then every `every` seconds until `minutes` have passed. Returns the passes run."""
    end = clock() + minutes * 60
    passes = 0
    while True:
        for line in dispatcher.locked_housekeeping() or []:
            dispatcher.log("   " + line)
        passes += 1
        if clock() + every >= end:
            return passes
        sleep(every)


def cmd_ingest(args) -> int:
    """Record readings from the desktop app's usage tool (JSON on stdin) - the director's own meter."""
    data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    observations = []
    for w in (data.get("plan") or data).get("windows", []):
        window = desktop_window(w.get("label", ""))
        if not window or w.get("percentUsed") is None:
            continue
        resets = datetime.fromisoformat(w["resetsAt"].replace("Z", "+00:00")).timestamp() if w.get("resetsAt") else None
        observations.append(usage.observation("claude", window, w["percentUsed"], resets, "desktop"))
    ledger = Ledger()
    ledger.record_usage(observations)
    ledger.close()
    print("recorded: " + ", ".join(f"{o['window']} {o['used_pct']:.0f}%" for o in observations))
    return 0


def describe_event(provider: str, line: str) -> list[str]:
    """One worker stream line -> readable lines (tool calls, text, results)."""
    try:
        e = json.loads(line)
    except ValueError:
        return [line.strip()[:200]] if line.strip() else []
    out, kind = [], e.get("type")
    if kind == "assistant":
        for b in (e.get("message") or {}).get("content") or []:
            if b.get("type") == "text" and b["text"].strip():
                out.append("say  " + b["text"].strip().replace(chr(10), " ")[:220])
            elif b.get("type") == "tool_use":
                out.append(f"tool {b['name']} " + json.dumps(b.get("input"))[:180])
    elif kind == "user":
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                c = b.get("content"); c = c if isinstance(c, str) else json.dumps(c)
                out.append("  !! " + c.replace(chr(10), " ")[:200])
    elif kind == "result":
        out.append(f"done is_error={e.get('is_error')} turns={e.get('num_turns')} cost~${e.get('total_cost_usd')}")
    elif kind == "item.completed":
        item = e.get("item") or {}
        if item.get("type") == "agent_message":
            out.append("say  " + item.get("text", "")[:220].replace(chr(10), " "))
        elif item.get("type") == "command_execution":
            out.append("cmd  " + str(item.get("command"))[:180] + f"  (exit {item.get('exit_code')})")
        elif item.get("type") == "file_change":
            out.append("edit " + json.dumps(item.get("changes"))[:180])
    elif kind in ("turn.failed", "error"):
        out.append("  !! " + json.dumps(e)[:220])
    return out


def cmd_peek(args) -> int:
    cfg = config_mod.load()
    folder = Ledger().path.parent / "logs" / (cfg["project"].get("name") or "default")
    logs = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not logs:
        print("no worker logs yet")
        return 0
    path = logs[-1 - args.back]
    provider = "codex" if "-codex-" in path.name else "claude"
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(raw)
        if rec["channel"] == "out":
            lines += describe_event(provider, rec["line"])
        elif rec["line"].strip():
            lines.append("err  " + rec["line"].strip()[:200])
    print(f"{path.name} ({len(lines)} events)")
    print(chr(10).join(lines[-args.lines:]))
    return 0


def efficiency(ledger, project, since: float, now: float) -> dict:
    """Are we getting the most out of the allowance? Output per unit of spend, and work that didn't land, by role,
    model and effort. `api_usd` is Claude's API-equivalent cost of each run, a proxy for its share of the plan.
    `wasted` is work that failed; `stopped` is work the room cut short (a hard pause, a reserve, a breaker, a dead
    dispatcher), counted apart so it isn't blamed on the role (commonroom#23)."""
    runs = [r for r in ledger.recent_runs(project, limit=2000) if r["started_at"] >= since]
    groups: dict = {}
    for r in runs:
        if r["role"] in metrics.ROOM_ROLES:
            continue
        g = groups.setdefault(f"{r['role']}/{r['provider']}:{r['model']}@{r.get('effort') or 'default'}",
                              {"runs": 0, "done": 0, "wasted": 0, "stopped": 0, "stopped_minutes": 0.0,
                               "api_usd": 0.0, "minutes": 0.0})
        minutes = ((r.get("ended_at") or now) - r["started_at"]) / 60
        g["runs"] += 1
        g["done"] += r["status"] == "done"
        g["wasted"] += r["status"] in ("failed", "blocked", "room-error")
        if r["status"] == "stopped":
            g["stopped"] += 1
            g["stopped_minutes"] = round(g["stopped_minutes"] + minutes, 1)
        g["api_usd"] = round(g["api_usd"] + (r.get("cost_usd") or 0), 2)
        g["minutes"] = round(g["minutes"] + minutes, 1)
    for g in groups.values():
        g["usd_per_done"] = round(g["api_usd"] / g["done"], 2) if g["done"] else None
    def weekly_spent(provider: str) -> float:
        periods: dict = {}
        for u in ledger.usage_history(provider, "seven_day", since):
            periods.setdefault(round((u["resets_at"] or 0) / 3600), [u["used_pct"], u["used_pct"]])[1] = u["used_pct"]
        return round(sum(max(0.0, b - a) for a, b in periods.values()), 1)
    claude_spent, codex_spent = weekly_spent("claude"), weekly_spent("codex")
    merges = sum(1 for r in runs if r["role"] == "merge")
    return {"merges": merges, "claude_weekly_pct_spent": claude_spent,
            "codex_weekly_pct_spent": codex_spent,
            "merges_per_weekly_pct": round(merges / claude_spent, 2) if claude_spent else None,
            "note": "Claude's weekly % is plan-wide: it includes the owner's own sessions and the director chat. "
                    "Codex's is its own seven_day window",
            "by_role_model_effort": groups}


def digest(cfg: dict, hours: float) -> dict:
    """Everything an auditor needs about the last `hours`, in one structure."""
    ledger, now = Ledger(), time.time()
    project, repo, goal = cfg["project"].get("name"), cfg["project"].get("repo"), cfg["goal"].get("milestone")
    since = now - hours * 3600
    runs = [r for r in ledger.recent_runs(project, limit=500) if r["started_at"] >= since]
    for r in runs:
        r["minutes"] = round(((r.get("ended_at") or now) - r["started_at"]) / 60, 1)
        r["weekly_delta"] = (round(r["weekly_end"] - r["weekly_start"], 2)
                             if r.get("weekly_end") is not None and r.get("weekly_start") is not None else None)
    # Every runnable role is listed, a never-run one as None, so a neglected role shows (commonroom#4).
    last_role = dict.fromkeys(runnable_roles(cfg))
    for r in ledger.recent_runs(project, limit=500):
        if last_role.get(r["role"]) is None:
            last_role[r["role"]] = r["started_at"]
    result = {
        "window_hours": hours, "now": now, "project": cfg["project"], "goal": goal,
        "runs": runs,
        "summary": {"runs": len([r for r in runs if r["role"] not in metrics.ROOM_ROLES]),
                    "merges": len([r for r in runs if r["role"] == "merge"]),
                    "failed": len([r for r in runs if r["status"] in ("failed", "blocked")]),
                    "no_output": len([r for r in runs if r["role"] not in metrics.ROOM_ROLES and not r["outputs"]
                                      and r["status"] not in ("running", "room-error", "limited", "stopped")]),
                    "limited": len([r for r in runs if r["status"] == "limited"]),
                    # Cut short by the room (pause, reserve, breaker, dead dispatcher), not failed by the agent.
                    "stopped_by_room": len([r for r in runs if r["status"] == "stopped"]),
                    "minutes_lost_to_stops": round(sum(r["minutes"] for r in runs if r["status"] == "stopped"), 1),
                    # ACTIONS claims checked against GitHub, and how many GitHub didn't show (commonroom#68).
                    "actions_reported": sum(r.get("actions_checked") or 0 for r in runs),
                    "actions_unverified": sum(len(r["unverified"]) for r in runs)},
        "efficiency": efficiency(ledger, project, since, now),
        # Per-merge cost over 7 and 30 days whatever `hours` is, with `over_target` naming any target missed (commonroom#211).
        "metrics": metrics.project_report(ledger, project, cfg, now),
        "hours_since_last": {role: round((now - t) / 3600, 1) if t is not None else None for role, t in last_role.items()},
        "usage": {p: {"verdict": vars(v), "windows": ledger.latest_usage(p)} for p, v in verdicts(ledger, cfg, now).items()},
        "breakers": ledger.open_breakers(project),
    }
    if progress_mod.declared(cfg):
        answer = progress_mod.landed(cfg, since if hours == 24 else now - 86400)
        result["landed"] = {"count": answer.count, "last_lines": answer.lines[-5:], "why": answer.why}
    running = ledger.running_runs(project)
    ledger.close()
    if repo:
        try:
            if goal:
                result["milestone"] = board.milestone(repo, goal)
            issues = board.issues(repo)
            result["open_issues"] = [{"number": i["number"], "title": i["title"], "labels": [l["name"] for l in i["labels"]],
                                      "milestone": (i.get("milestone") or {}).get("title")} for i in issues]
            result["open_prs"] = board.gh(["pr", "list", "--repo", repo, "--state", "open", "--json", "number,title,headRefName,labels"]) or []
            result["merged_prs"] = board.gh(["pr", "list", "--repo", repo, "--state", "merged", "--limit", "15",
                                             "--json", "number,title,mergedAt"]) or []
            # What housekeeping would release now (commonroom#63), so the auditor sees a claim nobody holds.
            result["stale_claims"] = stale_claims(issues, result["open_prs"], running, now,
                                                  stale_minutes=policy_mod.overrides(cfg).get("stale_claim_minutes"))
        except Exception as exc:
            result["board_error"] = str(exc)[:300]
    return result


def cmd_digest(args) -> int:
    if args.hours is None:   # no --hours: the project's own digest, for any agent (commonroom#253)
        return handshake.cmd_digest(args)
    print(json.dumps(digest(config_mod.load(), args.hours), indent=1, default=str))
    return 0


def cmd_pause(args) -> int:
    """Pause autonomous work with a reason (auditor or owner): nothing new starts, and running workers finish the job
    they're on (commonroom#23). `--hard` stops them too; their work is saved as WIP on the branch. Clear either from
    the dashboard or with `room breaker reset`."""
    cfg, ledger = config_mod.load(), Ledger()
    kind = args.kind + (gate.HARD if args.hard and not args.kind.endswith(gate.HARD) else "")
    ledger.trip(cfg["project"].get("name"), kind, args.reason)
    ledger.close()
    if gate.stops_running(kind):
        print(f"Autonomy stopped ({kind}): {args.reason}. Running workers stop now; unfinished work is saved on its branch.")
    else:
        print(f"Autonomy paused ({kind}): {args.reason}. Nothing new starts; running workers finish their current job.")
    return 0


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def cmd_services(args) -> int:
    """Start the project's long-running services if they aren't up: the dashboard, plus anything in [services]."""
    from commonroom import services
    cfg = config_mod.load()
    if not cfg.get("_root"):
        print("No room.toml found; run inside a project")
        return 2
    for line in services.ensure(cfg, Ledger().path.parent / "logs"):
        print(line)
    return 0


def cmd_tick(args) -> int:
    """Run the project's `[heartbeat] commands`: per-project jobs that ride the heartbeat (commonroom#151)."""
    from commonroom import services
    cfg = config_mod.load()
    if not cfg.get("_root"):
        print("No room.toml found; run inside a project")
        return 2
    for line in services.tick(cfg):
        print(time.strftime("%m-%d %H:%M:%S"), line, flush=True)
    return 0


def cmd_stop(args) -> int:
    """Ask a running dispatcher to exit after its current job (used to deploy new room code)."""
    cfg = config_mod.load()
    path = Ledger().path.parent / f"{cfg['project'].get('name') or 'default'}.stop"
    path.write_text(str(time.time()))
    print(f"Stop requested: the dispatcher exits after its current job ({path})")
    return 0


def cmd_dashboard(args) -> int:
    from commonroom.dashboard import serve
    cfg = config_mod.load()
    if not cfg.get("_root"):
        print("No room.toml found; run inside a project")
        return 2
    serve(cfg, args.port)
    return 0


def cmd_suggest(args) -> int:
    """Owner, in the room channel: an idea for the director to triage. It can be rejected."""
    cfg = config_mod.load()
    repo = cfg["project"].get("repo")
    body = (args.body or "") + "\n\n_Owner suggestion (room channel). The director triages it: accept, reject with reasons, or park._"
    cmd =["issue", "create", "--repo", repo, "--title", args.title, "--body", body.strip(), "--label", "kind:suggestion"]
    if cfg["goal"].get("milestone"):
        cmd += ["--milestone", cfg["goal"]["milestone"]]
    print(board.gh(cmd))
    return 0


def cmd_submit(args) -> int:
    """Owner, in the room channel: deliver an assigned task. Lists new files under the project's art/incoming."""
    import subprocess
    cfg = config_mod.load()
    root, repo = cfg.get("_root"), cfg["project"].get("repo")
    new = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "art/incoming"], cwd=root,
                         capture_output=True, text=True).stdout.splitlines() if root else []
    files = [line[3:] for line in new if line.strip()]
    body = "submitted" + (f": {args.note}" if args.note else "")
    if files:
        body += "\n\nNew files:\n" + "\n".join(f"- `{f}`" for f in files)
    board.gh(["issue", "comment", str(args.issue), "--repo", repo, "--body", body])
    print(f"Submitted on #{args.issue}" + (f" with {len(files)} new file(s)" if files else ""))
    return 0


def _run_limited(test: str, deadline: float) -> int | None:
    """Run the test command; None if it is still going at the deadline (its process tree is killed first)."""
    if time.monotonic() >= deadline:
        return None   # waiting for the lock may have used the whole allowance
    posix = os.name != "nt"
    process = subprocess.Popen(test, shell=True, cwd=os.getcwd(), start_new_session=posix)
    try:
        return process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        if posix:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        process.kill()
        process.wait()
        return None


def cmd_check(args) -> int:
    """Run the project's `[checks] test` under a per-project lock, so parallel workers queue instead of starving each other.

    The whole check, lock wait included, stops at `[checks] max_minutes`: exit 3 means the suite didn't finish."""
    cfg = config_mod.load()
    checks = cfg.get("checks") or {}
    test = checks.get("test")
    if not cfg.get("_root") or not test:
        print("No [checks] test in room.toml; nothing to run")
        return 2
    started = time.monotonic()
    max_minutes = float(checks.get("max_minutes", 9))
    limit = started + max_minutes * 60
    wait = float(checks.get("lock_wait_minutes", 5)) * 60
    poll = float(checks.get("lock_poll_seconds", 3))
    lock = suite_lock(ledger_mod.default_path().parent, _project_name(cfg))
    deadline, told = min(started + wait, limit), None

    def run() -> int:
        code = _run_limited(test, limit)
        if code is None:
            print(f"suite ran out of time after {max_minutes:g} min: report TESTS: INCOMPLETE", flush=True)
            return 3
        return code
    # A sandbox that can read the room home but not write it (Codex) fails on any claim, the first or a retry after
    # the holder lets go: run unlocked then, as the test command alone would.
    try:
        holder = claim_lock(lock)
        while holder is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("suite lock busy: report TESTS: INCOMPLETE", flush=True)
                return 3
            if holder != told:
                print(f"waiting for the suite lock (pid {holder})", flush=True)
                told = holder
            time.sleep(min(poll, remaining))
            if time.monotonic() < deadline:
                holder = claim_lock(lock)
    except OSError:
        print("suite lock unavailable here; running unlocked", flush=True)
        return run()
    try:
        return run()
    finally:
        release_lock(lock)


def cmd_breaker(args) -> int:
    cfg, ledger = config_mod.load(), Ledger()
    project = _project_name(cfg)
    if args.action == "reset":
        print(f"cleared {ledger.clear_breakers(project, args.kind)} breaker(s)")
    else:
        for b in ledger.open_breakers(project):
            print(f"{_clock(b['at'])} {b['kind']}: {b['detail']}")
    ledger.close()
    return 0


def cmd_projects(args) -> int:
    """List, add, change or remove the projects in the registry (`<room home>/projects.toml`). No model or GitHub call.
    A change that would break a rule is refused with the reason, and the file is left as it was."""
    path = registry.default_path()
    try:
        reg = registry.load(path, check=False)
    except registry.RegistryError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if args.action is None:
        if not reg.projects:
            print(f"no projects in {path}")
        for name in sorted(reg.projects):
            p = reg.projects[name]
            share = registry.fmt_number(p.share) if registry.is_number(p.share) else repr(p.share)
            print(f"{name:<16} share {share:>5}  {p.work}/{p.when}{'/strict' if p.strict is True else ''}  {p.path}")
        print(f"owner's own slice: {registry.fmt_number(reg.owner_share())}")
        found = registry.problems(reg)
        for problem in found:
            print(f"problem: {problem}")
        return 1 if found else 0
    name = args.name
    if args.action == "add":
        if any(n.lower() == name.lower() for n in reg.projects):
            print(f"refused: project {name!r} already exists; use `room projects set`", file=sys.stderr)
            return 1
        reg.projects[name] = registry.Project(name, os.path.abspath(args.path), args.share,
                                              args.work or "open", args.when or "paced", bool(args.strict))
    elif name not in reg.projects:
        print(f"refused: no project {name!r} in {path}", file=sys.stderr)
        return 1
    elif args.action == "remove":
        del reg.projects[name]
    else:
        p = reg.projects[name]
        for key in ("share", "work", "when", "strict"):
            if getattr(args, key) is not None:
                setattr(p, key, getattr(args, key))
    try:
        warnings = registry.save(reg, path, changing=name)
    except registry.RegistryError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"{args.action} {name}: saved {path}; owner's own slice is {registry.fmt_number(reg.owner_share())}")
    return 0


def cmd_pie_raise(args) -> int:
    if (args.clear and (args.points is not None or args.sources)) or (not args.clear and (args.name is None or args.points is None)):
        print("use: room pie raise <project> <points> [--from <project>...] or raise --clear [<project>]", file=sys.stderr)
        return 1
    lock = raises.path().with_suffix(".lock")
    if claim_lock(lock) is not None:
        print("refused: another pie raise write is in progress", file=sys.stderr)
        return 1
    ledger = None
    try:
        reg, now = registry.load(), time.time()
        ledger = Ledger()
        entries = raises.load()
        current = []
        for provider in PROVIDERS:
            win = standings.window(ledger, provider, now)
            if win:
                current.extend(raises.current(entries, win, standings.samples(ledger, win, now)))
        if args.clear:
            current = [e for e in current if not ((args.name is None or e["project"] == args.name)
                       and (args.provider is None or e["provider"] == args.provider))]
        else:
            providers = (args.provider,) if args.provider else PROVIDERS
            reports = [standings.standings(ledger, reg, p, now) for p in providers]
            if not any(reports) or (args.provider and reports[0] is None):
                raise registry.RegistryError("no current weekly reading for the requested provider(s)")
            for report in filter(None, reports):
                active = [e for e in current if e["provider"] == report.window.provider]
                entry = raises.transfer(reg, report, active, args.name, args.points, args.sources)
                raises.effective(reg, active + [entry])
                current.append(entry)
        for provider in PROVIDERS:
            raises.effective(reg, [e for e in current if e["provider"] == provider])
        raises.save(current)
        print(f"{'cleared raises' if args.clear else 'raised ' + args.name}: saved {raises.path()}")
        return 0
    except (registry.RegistryError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        if ledger is not None:
            ledger.close()
        release_lock(lock)


def _pie_json(report: standings.Report | None, now: float) -> dict | None:
    if report is None:
        return None
    w = report.window
    return {"resets_at": w.resets_at, "week_gone": w.elapsed, "used_pct": w.used_pct, "reading_at": w.observed_at,
            "reading_age_seconds": now - w.observed_at,
            "stale": now - w.observed_at > config_mod.DEFAULTS["freshness_seconds"],
            "projects_total_est": report.projects_total, "unregistered_est": report.unregistered,
            "owner_share": report.owner_share, "owner_use_est": report.owner_use,
            "projects": [{"name": s.name, "share": s.share, "when": s.when, "strict": s.strict, "spent_est": s.spent,
                          "room_est": report.room(s.name), "own_sessions_est": report.own.get(s.name, 0.0),
                          "line": s.allowed, "headroom_est": s.headroom} for s in report.rows],
            **({"raises": report.raises} if report.raises else {}),
            **({"away_until": report.away_until, "lent": report.lent,
                "lent_total": sum(report.lent.values())} if report.away_until is not None else {})}


def _weekly_json(found: list, moved) -> dict:
    return {"windows": [p.__dict__ for p in found], **({"switch": moved.__dict__} if moved else {})}


def _weekly_lines(found: list, moved) -> list[str]:
    """One line per weekly window with its projection, and what the room's latest move off one did (commonroom#256)."""
    lines = []
    for p in found:
        name = "all models" if p.window == weekly.ALL else weekly.model_part(p.window)
        if not p.known:
            lines.append(f"week ({name}): UNKNOWN, missing from the latest reading (last seen {_clock(p.observed_at)})")
            continue
        if p.rate_per_day is None:
            pace = "too few readings in the last 24 hours to project"
        elif p.runs_out_at is None:
            pace = f"+{p.rate_per_day:.1f} points a day over the last 24 hours: lasts the week"
        else:
            pace = (f"+{p.rate_per_day:.1f} points a day over the last 24 hours: runs out "
                    f"{datetime.fromtimestamp(p.runs_out_at).strftime('%a %d %b %H:%M')}")
        lines.append(f"week ({name}): {p.used_pct:.0f}% used, resets "
                     f"{datetime.fromtimestamp(p.resets_at).strftime('%a %d %b %H:%M')}; {pace}")
    if moved:
        moves = ", ".join(f"{w} {moved.before[w]:.0f}% -> {moved.after[w]:.0f}%" for w in sorted(moved.after))
        lines.append(f"switch: the room's work left {moved.window} at {_clock(moved.at)}; since then {moves}: "
                     f"{moved.outcome.upper()}")
    return lines


def cmd_pie(args) -> int:
    """Each project's effective share and standing. Spend is an estimate because the meter is plan-wide."""
    path, now = registry.default_path(), time.time()
    try:
        reg = registry.load(path)
        if not reg.projects:
            print(json.dumps({"registry": str(path), "providers": {}}) if args.json else f"no projects are registered in {path}")
            return 0
        ledger = Ledger()
        try:
            reports = {provider: standings.standings(ledger, reg, provider, now) for provider in PROVIDERS}
            weeks = {provider: weekly.windows(ledger, provider, now) for provider in PROVIDERS}
            switches = {provider: weekly.switch(ledger, provider, now, weeks[provider]) for provider in PROVIDERS}
        finally:
            ledger.close()
    except registry.RegistryError as exc:
        print(json.dumps({"registry": str(path), "error": str(exc)}) if args.json else f"problem: {exc}",
              file=sys.stdout if args.json else sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"registry": str(path), "providers": {p: _pie_json(r, now) for p, r in reports.items()},
                          "weekly": {p: _weekly_json(weeks[p], switches[p]) for p in PROVIDERS}}, indent=2))
        return 0
    print("est. = an estimate: the meter is plan-wide, so each project's part is inferred from its room runs and the "
          "owner's own Claude sessions in its folder")
    for provider, report in reports.items():
        if report is None:
            print(f"{provider}: no current weekly reading")
            continue
        w = report.window
        resets = datetime.fromtimestamp(w.resets_at).strftime("%a %d %b %H:%M")
        print(f"{provider}: {w.elapsed:.0%} of the week gone, resets {resets}; plan-wide {w.used_pct:.1f}% used")
        for line in _weekly_lines(weeks[provider], switches[provider]):
            print(f"  {line}")
        for e in report.raises:
            donors = ", ".join(f"{k} {v:g}" for k, v in e["from"].items())
            print(f"  raise {e['project']} +{sum(e['from'].values()):g} from {donors}; ends {resets} (or early reset)")
        if now - w.observed_at > config_mod.DEFAULTS["freshness_seconds"]:
            print(f"  STALE: the latest reading is from {_clock(w.observed_at)}, {_ago(now - w.observed_at)} old; "
                  "use since then is not counted, and the lines are for now")
        if report.away_until is not None:
            print(f"  owner AWAY until {datetime.fromtimestamp(report.away_until).strftime('%a %d %b %H:%M')}: "
                  f"{sum(report.lent.values()):.1f} points of his slice lent ("
                  + ", ".join(f"{k} +{v:.1f}" for k, v in sorted(report.lent.items())) + ")")
        others = "".join(f"; {name} (not registered) est. {pct:.1f}%" for name, pct in sorted(report.unregistered.items()))
        print(f"  projects est. {report.projects_total:.1f}%{others}; owner's slice "
              f"{registry.fmt_number(report.owner_share)}, his own use est. {report.owner_use:.1f}%")
        print(f"  {'project':<16} {'share':>5}  {'when':<6} {'spent est.':>10} {'room':>6} {'own sessions':>12} "
              f"{'line':>6} {'headroom est.':>13}")
        for s in report.rows:
            print(f"  {s.name:<16} {registry.fmt_number(s.share):>5}  {s.when:<6} {s.spent:>10.1f} "
                  f"{report.room(s.name):>6.1f} {report.own.get(s.name, 0.0):>12.1f} {s.allowed:>6.1f} {s.headroom:>+13.1f}")
        for s in report.rows:
            if pie_notice.over(report, s.name):
                print(f"  OVER ITS SHARE: {pie_notice.text(reg, report, s.name, s)}")
    return 0


def cmd_away(args) -> int:
    """`room away until <date>`, `room away`, `room away --clear`: the owner's return, kept beside the registry. It only
    changes shares and reserves; it never resumes, clears a breaker or starts anything."""
    words, now = args.words, time.time()
    try:
        if args.clear:
            print("away cleared" if away.clear() else "not away: nothing to clear")
            return 0
        if not words:
            until = away.load()
            if until is None or until <= now:
                print("not away" + (" (the stored return time has passed)" if until else ""))
            else:
                print(f"away until {datetime.fromtimestamp(until).strftime('%a %d %b %Y %H:%M')}; `room pie` shows "
                      "how many points are lent")
            return 0
        if words[0] != "until" or len(words) < 2:
            print("use: room away until <date or 'date time'>, room away, or room away --clear", file=sys.stderr)
            return 1
        until = away.parse(" ".join(words[1:]))
        if until <= now:
            raise registry.RegistryError("that time has already passed")
        away.save(until)
        print(f"away until {datetime.fromtimestamp(until).strftime('%a %d %b %Y %H:%M')}: saved {away.path()}")
        return 0
    except (registry.RegistryError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


def cmd_reddit(args) -> int:
    """Read-only Reddit research through public archives. Small reads only: no bulk collection, never training."""
    from commonroom import reddit
    try:
        results = reddit.search(args.query, subreddit=args.sub, comments=args.comments, limit=args.limit)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"no archive answered: {exc}")
        return 1
    if args.json:
        print(json.dumps(results, indent=1))
        return 0
    for r in results:
        print(f"{r['date']}  r/{r['subreddit']}  score {r['score']}  {r['title'][:100]}\n    {r['url']}")
        if r["excerpt"]:
            print(f"    {r['excerpt'][:200]}")
    print(f"{len(results)} result(s) via arctic-shift")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="room", description="Common Room v2")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tap", help="status-line command: record Claude rate limits").set_defaults(func=cmd_tap)
    p = sub.add_parser("usage", help="latest readings and gate verdicts"); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_usage)
    p = sub.add_parser("gate", help="exit 0 if autonomous work may use this provider"); p.add_argument("provider", choices=PROVIDERS); p.set_defaults(func=cmd_gate)
    sub.add_parser("beat", help="one heartbeat over every registered project").set_defaults(func=cmd_beat)
    p = sub.add_parser("probe", help="read meters without inference"); p.add_argument("provider", nargs="?", default="all", choices=(*PROVIDERS, "all")); p.set_defaults(func=cmd_probe)
    sub.add_parser("status", help="goal, owner queue, usage, runs, breakers").set_defaults(func=cmd_status)
    p = sub.add_parser("run", help="dispatch work from the board"); p.add_argument("--loop", action="store_true"); p.add_argument("--until-idle", action="store_true"); p.add_argument("--provider", choices=PROVIDERS); p.add_argument("--interval", type=int, default=300); p.add_argument("--dry-run", action="store_true"); p.add_argument("--slot", type=int, default=1, help="2+: an extra parallel builder for --provider"); p.set_defaults(func=cmd_run)
    p = sub.add_parser("digest", help="rebuild and print the project's digest; --hours N: JSON summary for the auditor"); g = p.add_mutually_exclusive_group(); g.add_argument("--hours", type=float); g.add_argument("--project"); p.add_argument("name", nargs="?", help="the project (same as --project)"); p.set_defaults(func=cmd_digest)
    p = sub.add_parser("attach", help="join a project: make its room folder, record who attached, print the digest (no model call)"); p.add_argument("name", nargs="?", help="the project (same as --project)"); p.add_argument("--project"); p.add_argument("--as", dest="who"); p.set_defaults(func=handshake.cmd_attach)
    p = sub.add_parser("note", help="leave a note in a project's inbox ('-' reads the text from standard input)"); p.add_argument("words", nargs="+", metavar="[project] text"); p.add_argument("--project"); p.add_argument("--from", dest="sender"); p.add_argument("--to"); p.set_defaults(func=handshake.cmd_note)
    p = sub.add_parser("notes", help="a project's notes, newest first"); p.add_argument("name", nargs="?", help="the project (same as --project)"); p.add_argument("--project"); p.add_argument("--since", help="6h, 3d, 2026-11-20 or '2026-11-20 14:00'"); p.set_defaults(func=handshake.cmd_notes)
    p = sub.add_parser("pause", help="pause autonomous work with a reason: nothing new starts, running workers finish"); p.add_argument("reason"); p.add_argument("--kind", default="paused_by_auditor", help="kinds starting with 'pause' let running workers finish"); p.add_argument("--hard", action="store_true", help="also stop running workers now (a runaway, a bad merge)"); p.set_defaults(func=cmd_pause)
    p = sub.add_parser("housekeep", help="merge approved PRs and unblock issues, with no job and no gate"); p.add_argument("--minutes", type=float, default=14); p.add_argument("--every", type=float, default=180); p.set_defaults(func=cmd_housekeep)
    sub.add_parser("services", help="start the dashboard and project services if they aren't running").set_defaults(func=cmd_services)
    sub.add_parser("tick", help="run the project's [heartbeat] commands once").set_defaults(func=cmd_tick)
    sub.add_parser("stop", help="ask the running dispatcher to exit after its current job").set_defaults(func=cmd_stop)
    p = sub.add_parser("dashboard", help="local dashboard on http://127.0.0.1:8770"); p.add_argument("--port", type=int, default=8770); p.set_defaults(func=cmd_dashboard)
    p = sub.add_parser("suggest", help="owner: propose an idea for the director to triage"); p.add_argument("title"); p.add_argument("--body"); p.set_defaults(func=cmd_suggest)
    p = sub.add_parser("submit", help="owner: deliver an assigned task"); p.add_argument("issue", type=int); p.add_argument("--note"); p.set_defaults(func=cmd_submit)
    p = sub.add_parser("reddit", help="read-only Reddit research through public archives (small reads only)"); p.add_argument("query"); p.add_argument("--sub", help="subreddit (required: Arctic Shift needs one)"); p.add_argument("--comments", action="store_true"); p.add_argument("--limit", type=int, default=25); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_reddit)
    p = sub.add_parser("peek", help="readable tail of the latest worker log"); p.add_argument("-n", "--lines", type=int, default=40); p.add_argument("--back", type=int, default=0); p.set_defaults(func=cmd_peek)
    sub.add_parser("ingest", help="record desktop usage-tool JSON from stdin").set_defaults(func=cmd_ingest)
    p = sub.add_parser("hook", help="Claude Code hook: session-start adds a pointer to the project's room folder (hook JSON on stdin)"); p.add_argument("event", nargs="?"); p.set_defaults(func=hook.cmd_hook)
    sub.add_parser("guard-command", help="PreToolUse hook: block filter-based process kills (hook JSON on stdin)").set_defaults(func=cmd_guard_command)
    sub.add_parser("check", help="run the project's [checks] test under a per-project lock").set_defaults(func=cmd_check)
    p = sub.add_parser("breaker", help="list or reset breakers"); p.add_argument("action", choices=("list", "reset")); p.add_argument("--kind"); p.set_defaults(func=cmd_breaker)
    p = sub.add_parser("projects", help="list or change the projects and their shares (no model call)"); p.set_defaults(func=cmd_projects)
    ps = p.add_subparsers(dest="action")
    q = ps.add_parser("add", help="add a project"); q.add_argument("name"); q.add_argument("path"); q.add_argument("--share", type=float, required=True)
    q.add_argument("--work", choices=registry.WORK); q.add_argument("--when", choices=registry.WHEN); q.add_argument("--strict", action=argparse.BooleanOptionalAction)
    q = ps.add_parser("set", help="change a project's share or mode"); q.add_argument("name"); q.add_argument("--share", type=float)
    q.add_argument("--work", choices=registry.WORK); q.add_argument("--when", choices=registry.WHEN); q.add_argument("--strict", action=argparse.BooleanOptionalAction)
    q = ps.add_parser("remove", help="remove a project"); q.add_argument("name")
    p = sub.add_parser("away", help="tell the room when the owner is back, so his unneeded slice is lent (no model call)"); p.add_argument("words", nargs="*", metavar="until <date>"); p.add_argument("--clear", action="store_true"); p.set_defaults(func=cmd_away)
    p = sub.add_parser("pie", help="each project's effective share and weekly standing (estimates)"); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_pie)
    ps = p.add_subparsers(dest="action")
    q = ps.add_parser("raise", help="raise a project's share for the current weekly windows only")
    q.add_argument("name", nargs="?"); q.add_argument("points", nargs="?", type=float)
    q.add_argument("--from", dest="sources", nargs="+", action="extend", help="donor projects; @owner names the unassigned slice")
    q.add_argument("--clear", action="store_true"); q.add_argument("--provider", choices=PROVIDERS)
    q.set_defaults(func=cmd_pie_raise)
    args, extra = parser.parse_known_args(argv)
    if extra and args.func is handshake.cmd_note and not any(x.startswith("-") and x != "-" for x in extra):
        args.words += extra    # argparse ends a `+` positional at the first option: words after `--from X` still belong to it
    elif extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
