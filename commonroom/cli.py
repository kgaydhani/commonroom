"""`room` command line: usage, gate, status and the status-line tap."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from commonroom import board, config as config_mod, gate, usage
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


def verdicts(ledger: Ledger, cfg: dict, now: float | None = None) -> dict[str, gate.Verdict]:
    now = time.time() if now is None else now
    result = {}
    project = _project_name(cfg)
    runs = ledger.recent_runs(project, limit=cfg["breakers"]["no_output_runs"], autonomous=True, finished=True)
    breaker, trip = gate.breaker_verdict(ledger.open_breakers(project), runs, cfg["breakers"]["no_output_runs"])
    if trip:
        ledger.trip(project, trip["kind"], trip["detail"], now=now)
    for provider in PROVIDERS:
        v = gate.usage_verdict(provider, ledger.latest_usage(provider), config_mod.budget(cfg, provider), now,
                               cfg["freshness_seconds"])
        result[provider] = v if (v.allowed is False or breaker.allowed) else gate.Verdict(False, breaker.reason, v.detail)
    return result


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
    p = sub.add_parser("breaker", help="list or reset breakers"); p.add_argument("action", choices=("list", "reset")); p.add_argument("--kind"); p.set_defaults(func=cmd_breaker)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
