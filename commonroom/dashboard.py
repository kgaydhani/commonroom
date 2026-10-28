"""Local dashboard: what's running, what it costs, what it produced, and the owner's queue.

`room dashboard` serves http://127.0.0.1:8770. Read-mostly; the only actions are pause/resume and suggestions.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from commonroom import board, config as config_mod
from commonroom.dispatch import verdicts
from commonroom.ledger import Ledger

PAUSE_KIND = "paused_by_owner"
_cache: dict = {}


def cached(key: str, seconds: float, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    try:
        value = fetch()
    except Exception as exc:
        value = {"error": str(exc)[:300]}
    _cache[key] = (time.time(), value)
    return value


def worker_tail(ledger: Ledger, project: str, lines: int = 18) -> dict | None:
    from commonroom.cli import describe_event
    folder = ledger.path.parent / "logs" / project
    logs = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    if not logs:
        return None
    path = logs[-1]
    events = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(raw)
        if rec["channel"] == "out":
            events += describe_event("codex" if "-codex-" in path.name else "claude", rec["line"])
    return {"file": path.name, "updated": path.stat().st_mtime, "events": events[-lines:], "count": len(events)}


def owner_queue(repo: str, trusted: set[str]) -> list[dict]:
    """Owner tasks with their brief and the conversation so far, soonest due first. Only the trusted logins' comments
    (commonroom#176): on a public repo anyone can comment, and a stranger's words must never show as the owner's."""
    rows = board.gh(["issue", "list", "--repo", repo, "--label", board.OWNER_LABEL, "--state", "open", "--limit", "50",
                     "--json", "number,title,body,comments,url"]) or []
    queue = []
    for i in rows:
        comments = [{"who": "agent" if (c.get("body") or "").lstrip().startswith("[") else "you",
                     "body": (c.get("body") or "")[:600], "at": c.get("createdAt")} for c in i.get("comments") or []
                    if str((c.get("author") or {}).get("login") or "").lower() in trusted]
        queue.append({"number": i["number"], "title": i["title"], "due": board.due_date(i["body"]), "url": i["url"],
                      "brief": (i.get("body") or "")[:1500], "comments": comments[-4:]})
    return sorted(queue, key=lambda q: q["due"] or "9999")


def latest_report(ledger: Ledger, project: str | None) -> dict | None:
    """The auditor's newest status report. Its first line is `VERDICT: <ON TRACK|AT RISK|OFF TRACK> - <headline>`."""
    path = ledger.path.parent / "reports" / (project or "default") / "latest.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    first, _, rest = text.partition("\n")
    verdict, headline = "REPORT", first
    if first.upper().startswith("VERDICT:"):
        body = first.split(":", 1)[1].strip()
        for v in ("OFF TRACK", "AT RISK", "ON TRACK"):
            if body.upper().startswith(v):
                verdict, headline = v, body[len(v):].lstrip(" -—:")
                break
    return {"verdict": verdict, "headline": headline, "body": rest.strip(), "updated": path.stat().st_mtime}


def state(cfg: dict) -> dict:
    ledger = Ledger()
    try:
        project = cfg["project"].get("name")
        repo, goal = cfg["project"].get("repo"), cfg["goal"].get("milestone")
        now = time.time()
        usage_state = {}
        for provider, v in verdicts(ledger, cfg, now).items():
            usage_state[provider] = {"allowed": v.allowed, "reason": v.reason, "detail": v.detail,
                                     "windows": ledger.latest_usage(provider)}
        runs = ledger.recent_runs(project, limit=25)
        day = [r for r in ledger.recent_runs(project, limit=400) if r["started_at"] >= now - 86400]
        model_runs = [r for r in day if r["role"] not in ("merge", "update") and r["status"] != "room-error"]
        merges = [r for r in day if r["role"] == "merge"]
        proof = {"merged_24h": len(merges), "runs_24h": len(model_runs),
                 "productive_24h": sum(1 for r in model_runs if r["outputs"]),
                 "failed_24h": sum(1 for r in model_runs if r["status"] in ("failed", "blocked")),
                 "last_merge_at": max((r["started_at"] for r in merges), default=None),
                 "weekly_spent_24h": round(sum(max(0.0, (r.get("weekly_end") or 0) - (r.get("weekly_start") or 0))
                                               for r in model_runs if r.get("weekly_start") is not None), 1)}
        result = {
            "report": latest_report(ledger, project),
            "proof": proof,
            "now": now, "version": VERSION, "project": cfg["project"], "goal": goal, "usage": usage_state, "runs": runs,
            "running": [r for r in runs if r["status"] == "running"],
            "breakers": ledger.open_breakers(project),
            "worker": worker_tail(ledger, project) if project else None,
        }
        if repo:
            result["milestone"] = cached("milestone", 60, lambda: board.milestone(repo, goal) if goal else None)
            result["owner_queue"] = cached("owner", 30, lambda: owner_queue(repo, config_mod.trusted(cfg)))
            result["board"] = cached("board", 45, lambda: [
                {"number": i["number"], "title": i["title"], "labels": [l["name"] for l in i["labels"]],
                 "milestone": (i.get("milestone") or {}).get("title")}
                for i in board.issues(repo)])
        return result
    finally:
        ledger.close()


PAGE = (Path(__file__).with_name("static") / "dashboard.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    cfg: dict = {}

    def log_message(self, *args):
        pass

    def send(self, code: int, body: bytes, kind: str):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self.send(200, json.dumps(state(self.cfg), default=str).encode(), "application/json")
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self):
        # A custom header forces a CORS preflight, so other sites can't drive these endpoints.
        if self.headers.get("X-Room") != "1":
            return self.send(403, b"forbidden", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        project = self.cfg["project"].get("name")
        ledger = Ledger()
        try:
            if self.path == "/api/pause":
                ledger.trip(project, PAUSE_KIND, "paused from the dashboard")
                result = {"ok": True}
            elif self.path == "/api/resume":
                # Resume clears every stop (owner pause, auditor pause, breakers): the owner has decided to continue.
                result = {"ok": True, "cleared": ledger.clear_breakers(project)}
            elif self.path == "/api/comment":
                number, text = int(body.get("number", 0)), str(body.get("text", "")).strip()[:4000]
                if not number or not text:
                    raise ValueError("Nothing to send")
                if text.startswith("["):
                    text = "​" + text  # leading "[" marks agent comments; keep owner comments distinguishable
                board.gh(["issue", "comment", str(number), "--repo", self.cfg["project"]["repo"], "--body", text])
                _cache.pop("owner", None)
                result = {"ok": True}
            elif self.path == "/api/suggest":
                text = str(body.get("title", "")).strip()[:8000]
                if not text:
                    raise ValueError("Write your idea first")
                # The full text goes in the body; the title is a short lead-in (titles get truncated).
                title = text if len(text) <= 90 else text[:90].rsplit(" ", 1)[0] + "…"
                args = ["issue", "create", "--repo", self.cfg["project"]["repo"], "--title", title, "--label", "kind:suggestion",
                        "--body", text + "\n\n_Owner suggestion from the dashboard (room channel). The director triages it: accept, reject with reasons, or park._"]
                if self.cfg["goal"].get("milestone"):
                    args += ["--milestone", self.cfg["goal"]["milestone"]]
                result = {"url": board.gh(args)}
                _cache.pop("board", None)
            else:
                return self.send(404, b"not found", "text/plain")
        except Exception as exc:
            result = {"error": str(exc)[:300]}
        finally:
            ledger.close()
        self.send(200, json.dumps(result).encode(), "application/json")


def serve(cfg: dict, port: int = 8770):
    Handler.cfg = cfg
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Common Room dashboard: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# Open pages reload themselves when this changes: a new page or a restarted server.
VERSION = hashlib.sha1((PAGE + str(time.time())).encode()).hexdigest()[:10]
