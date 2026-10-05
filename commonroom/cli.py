"""`room` command line: usage, gate, status and the status-line tap."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from commonroom import board, config as config_mod, gate, usage
from commonroom.dispatch import Dispatcher, verdicts
from commonroom.ledger import Ledger

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
            cfg = config_mod.load()
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
    print(" | ".join(parts) or "room")
    return 0


def cmd_usage(args) -> int:
    ledger, cfg, now = Ledger(), config_mod.load(), time.time()
    v = verdicts(ledger, cfg, now)
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
    ledger = Ledger()
    v = verdicts(ledger, config_mod.load())[args.provider]
    ledger.close()
    print(("GO " if v.allowed else "HOLD ") + v.reason)
    return 0 if v.allowed else 1


def cmd_probe(args) -> int:
    ledger, failures = Ledger(), 0
    targets = PROVIDERS if args.provider == "all" else (args.provider,)
    for provider in targets:
        try:
            observations = usage.codex_limits() if provider == "codex" else usage.claude_screen_limits()
            ledger.record_usage(observations)
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
    for provider, v in verdicts(ledger, cfg, now).items():
        print(f"{provider}: {'GO' if v.allowed else 'HOLD'} - {v.reason}")
    runs = ledger.recent_runs(project.get("name"), limit=5)
    if runs:
        print("Recent runs:")
        for r in runs:
            outputs = ", ".join(o.get("ref", o.get("kind", "?")) for o in r["outputs"]) or "no output"
            print(f"  {_clock(r['started_at'])} {r['role']}/{r['provider']} {r['task'] or '-'} {r['status']} -> {outputs}")
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
    lock = ledger.path.parent / f"{cfg['project'].get('name') or 'default'}.lock"
    if not args.dry_run and not acquire_lock(lock):
        print(f"another dispatcher holds {lock}; exiting")
        ledger.close()
        return 0
    dispatcher = Dispatcher(cfg, ledger, dry_run=args.dry_run, log=lambda line: print(f"{datetime.now():%H:%M:%S} {line}", flush=True))
    stop_file = ledger.path.parent / f"{cfg['project'].get('name') or 'default'}.stop"
    try:
        while True:
            report = dispatcher.run_once()
            if stop_file.exists():
                stop_file.unlink(missing_ok=True)
                print(f"{datetime.now():%H:%M:%S} stop requested; exiting between jobs", flush=True)
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


def acquire_lock(path) -> bool:
    """One dispatcher per project. A lock left by a dead process is taken over."""
    import os
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            if pid != os.getpid() and _alive(pid):
                return False
        except ValueError:
            pass
    path.write_text(str(os.getpid()))
    return True


def _alive(pid: int) -> bool:
    import subprocess
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def desktop_window(label: str) -> str | None:
    label = label.lower()
    if "5-hour" in label or "session" in label:
        return "five_hour"
    if label.startswith("weekly"):
        return "seven_day" if "all models" in label else "seven_day_" + label.split()[-1]
    return None


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


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def cmd_services(args) -> int:
    """Start the project's long-running services if they aren't up: the dashboard, plus anything in [services]."""
    import subprocess
    cfg = config_mod.load()
    root = cfg.get("_root")
    if not root:
        print("No room.toml found; run inside a project")
        return 2
    room_py = str(Path(__file__).resolve().parent.parent / "room.py")
    services = {"dashboard": {"command": [sys.executable, room_py, "dashboard"], "port": 8770}}
    for name, spec in (cfg.get("services") or {}).items():
        services[name] = {"command": spec["command"], "port": spec.get("port")}
    logs = Ledger().path.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW gives the service a hidden console that its children inherit. DETACHED_PROCESS would
        # make a shell's child allocate a fresh, visible console (Windows Terminal pops up).
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    for name, spec in services.items():
        if spec.get("port") and _port_open(int(spec["port"])):
            print(f"{name}: already running on :{spec['port']}")
            continue
        log = open(logs / f"service-{name}.log", "a", encoding="utf-8")
        command = spec["command"]
        subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         shell=isinstance(command, str), creationflags=flags, close_fds=True)
        print(f"{name}: started" + (f" on :{spec['port']}" if spec.get("port") else ""))
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
    p = sub.add_parser("probe", help="read meters without inference"); p.add_argument("provider", nargs="?", default="all", choices=(*PROVIDERS, "all")); p.set_defaults(func=cmd_probe)
    sub.add_parser("status", help="goal, owner queue, usage, runs, breakers").set_defaults(func=cmd_status)
    p = sub.add_parser("run", help="dispatch work from the board"); p.add_argument("--loop", action="store_true"); p.add_argument("--until-idle", action="store_true"); p.add_argument("--interval", type=int, default=300); p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_run)
    sub.add_parser("services", help="start the dashboard and project services if they aren't running").set_defaults(func=cmd_services)
    sub.add_parser("stop", help="ask the running dispatcher to exit after its current job").set_defaults(func=cmd_stop)
    p = sub.add_parser("dashboard", help="local dashboard on http://127.0.0.1:8770"); p.add_argument("--port", type=int, default=8770); p.set_defaults(func=cmd_dashboard)
    p = sub.add_parser("suggest", help="owner: propose an idea for the director to triage"); p.add_argument("title"); p.add_argument("--body"); p.set_defaults(func=cmd_suggest)
    p = sub.add_parser("submit", help="owner: deliver an assigned task"); p.add_argument("issue", type=int); p.add_argument("--note"); p.set_defaults(func=cmd_submit)
    p = sub.add_parser("peek", help="readable tail of the latest worker log"); p.add_argument("-n", "--lines", type=int, default=40); p.add_argument("--back", type=int, default=0); p.set_defaults(func=cmd_peek)
    sub.add_parser("ingest", help="record desktop usage-tool JSON from stdin").set_defaults(func=cmd_ingest)
    p = sub.add_parser("breaker", help="list or reset breakers"); p.add_argument("action", choices=("list", "reset")); p.add_argument("--kind"); p.set_defaults(func=cmd_breaker)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
