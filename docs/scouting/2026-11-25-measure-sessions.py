"""Figures for docs/scouting/2026-11-25-fresh-session-keeps-his-words.md (commonroom#626).

Reads Claude Code session records under ~/.claude/projects read-only and prints counts and token figures only:
no message text, titles, folder names or ids. Run: python -I docs/scouting/2026-11-25-measure-sessions.py
"""
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def typed_text(d):
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary") or d.get("isSidechain"):
        return None
    m = d.get("message") or {}
    if m.get("role") != "user":
        return None
    c = m.get("content")
    if isinstance(c, str):
        parts = [c]
    elif isinstance(c, list):
        if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c):
            return None
        parts = [p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"]
    else:
        return None
    t = "\n".join(parts).strip()
    if not t or t.startswith("<"):  # commands, reminders, hook output, task notifications
        return None
    return t


def session(path):
    entry, typed, compactions, reqs, seen = None, [], 0, [], set()
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        entry = entry or d.get("entrypoint")
        if d.get("isCompactSummary") or (d.get("type") == "system" and d.get("subtype") == "compact_boundary"):
            compactions += d.get("type") == "system"
        t = typed_text(d)
        if t is not None:
            typed.append(len(t))
        if d.get("type") == "assistant" and not d.get("isSidechain") and d.get("timestamp"):
            rid = d.get("requestId")
            u = (d.get("message") or {}).get("usage") or {}
            if not u or rid in seen:
                continue
            seen.add(rid)
            cr, cw, inp = (u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
                           u.get("input_tokens") or 0)
            reqs.append((ts(d["timestamp"]), inp + cr + cw, cr, cw, u.get("output_tokens") or 0))
    return entry, typed, compactions, sorted(reqs)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0


def main():
    files = [p for p in ROOT.glob("*/*.jsonl")]
    groups = {}
    for p in files:
        e, typed, comp, reqs = session(p)
        if not reqs:
            continue
        groups.setdefault("interactive" if e == "cli" else f"headless:{e}", []).append((typed, comp, reqs))
    for name, ss in sorted(groups.items()):
        maxctx = [max(r[1] for r in s[2]) for s in ss]
        chars = [sum(s[0]) for s in ss]
        life_h = [(s[2][-1][0] - s[2][0][0]) / 3600 for s in ss]
        print(f"\n== {name}: {len(ss)} sessions")
        print(f"  peak context tokens: median {statistics.median(maxctx):,.0f}  p90 {pct(maxctx, .9):,}  max {max(maxctx):,}")
        print(f"  sessions peaking over 150k: {sum(m > 150_000 for m in maxctx)}  over 400k: {sum(m > 400_000 for m in maxctx)}")
        print(f"  lifetime hours: median {statistics.median(life_h):.1f}  p90 {pct(life_h, .9):.1f}")
        print(f"  typed messages: {sum(len(s[0]) for s in ss):,}  typed chars total {sum(chars):,}  "
              f"per session median {statistics.median(chars):,.0f}  p90 {pct(chars, .9):,}  max {max(chars):,}")
        big = [(c, m) for c, m in zip(chars, maxctx) if m > 150_000]
        if big:
            ratio = [c / 4 / m for c, m in big]
            print(f"  sessions over 150k: typed tokens (chars/4) as share of peak context: median {statistics.median(ratio):.2%}  "
                  f"max {max(ratio):.2%}; typed chars median {statistics.median([c for c, _ in big]):,.0f}  max {max(c for c, _ in big):,}")
        print(f"  compactions: {sum(s[1] for s in ss)} in {sum(s[1] > 0 for s in ss)} sessions")
        # Gaps between consecutive main-chain requests, and what the request after the gap paid.
        buckets = {"5-55 min": [], "55-65 min": [], "65 min+": []}
        tot_read = tot_write = 0
        for s in ss:
            for a, b in zip(s[2], s[2][1:]):
                tot_read += b[2]
                tot_write += b[3]
                gap = (b[0] - a[0]) / 60
                key = "5-55 min" if 5 <= gap < 55 else "55-65 min" if 55 <= gap < 65 else "65 min+" if gap >= 65 else None
                if key:
                    buckets[key].append(b)
        for key, rs in buckets.items():
            if not rs:
                continue
            cold = [r for r in rs if r[3] > 0.5 * r[1]]
            print(f"  after a {key} gap: {len(rs)} returns, {len(cold)} rewrote over half the context; "
                  f"context at return median {statistics.median(r[1] for r in rs):,.0f}  p90 {pct([r[1] for r in rs], .9):,}")
        cold = [r for r in buckets["65 min+"] if r[3] > 0.5 * r[1]]
        cold_write = sum(r[3] for r in cold)
        print(f"  cache writes on cold returns: {cold_write:,} of {tot_write:,} total writes ({cold_write / max(tot_write, 1):.1%}); "
              f"total cache reads {tot_read:,}")
        # Price shares at Opus 5.5 list rates: 1h write $8/M, read $0.20/M (pricing page, read 25 Nov 2026).
        cost_cold, cost_all = cold_write * 8, tot_write * 8 + tot_read * 0.2
        print(f"  cold-return writes as share of input spend at Opus 5.5 list rates: {cost_cold / max(cost_all, 1):.1%}")
        print(f"  cold returns over 150k context: {sum(r[1] > 150_000 for r in cold)}; under 150k: {sum(r[1] <= 150_000 for r in cold)}")


if __name__ == "__main__":
    sys.exit(main())
