"""Local dashboard: what's running, what it costs, what it produced, and the owner's queue.

`room dashboard` serves http://127.0.0.1:8770. Read-mostly; the only actions are pause/resume and suggestions.
"""
from __future__ import annotations

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


def owner_queue(repo: str) -> list[dict]:
    """Owner tasks with their brief and the conversation so far, soonest due first."""
    rows = board.gh(["issue", "list", "--repo", repo, "--label", board.OWNER_LABEL, "--state", "open", "--limit", "50",
                     "--json", "number,title,body,comments,url"]) or []
    queue = []
    for i in rows:
        comments = [{"who": "agent" if (c.get("body") or "").lstrip().startswith("[") else "you",
                     "body": (c.get("body") or "")[:600], "at": c.get("createdAt")} for c in i.get("comments") or []]
        queue.append({"number": i["number"], "title": i["title"], "due": board.due_date(i["body"]), "url": i["url"],
                      "brief": (i.get("body") or "")[:1500], "comments": comments[-4:]})
    return sorted(queue, key=lambda q: q["due"] or "9999")


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
        result = {
            "now": now, "project": cfg["project"], "goal": goal, "usage": usage_state, "runs": runs,
            "running": [r for r in runs if r["status"] == "running"],
            "breakers": ledger.open_breakers(project),
            "worker": worker_tail(ledger, project) if project else None,
        }
        if repo:
            result["milestone"] = cached("milestone", 60, lambda: board.milestone(repo, goal) if goal else None)
            result["owner_queue"] = cached("owner", 30, lambda: owner_queue(repo))
            result["board"] = cached("board", 60, lambda: [
                {"number": i["number"], "title": i["title"], "labels": [l["name"] for l in i["labels"]]}
                for i in board.issues(repo, milestone=goal)] if goal else [])
        return result
    finally:
        ledger.close()


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Common Room</title><style>
:root{--bg:#f6f5f1;--card:#fff;--ink:#1f1e1b;--muted:#6b6a64;--line:#e3e1d9;--go:#1d9e75;--hold:#ba7517;--stop:#c0392b;--bar:#d9d6cc}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1a;--card:#262624;--ink:#ecebe6;--muted:#a3a29b;--line:#3a3936;--bar:#3a3936}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
h1{font-size:20px;margin:16px auto 0;max-width:1100px;padding:0 16px;font-weight:600}h2{font-size:14px;margin:0 0 8px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.wide{grid-column:1/-1}.muted{color:var(--muted)}.go{color:var(--go)}.hold{color:var(--hold)}.stop{color:var(--stop)}
.bar{position:relative;height:10px;background:var(--bar);border-radius:5px;margin:4px 0 10px}.bar>i{position:absolute;inset:0 auto 0 0;border-radius:5px;background:var(--ink)}
.bar>b{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--hold)}
pre{margin:0;white-space:pre-wrap;font:12px/1.45 ui-monospace,Consolas,monospace;max-height:340px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:4px 6px;border-top:1px solid var(--line);vertical-align:top}
button,input{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}button{cursor:pointer}
form{display:flex;gap:8px}form input{flex:1;min-width:0}.tag{font-size:11px;padding:1px 6px;border-radius:6px;border:1px solid var(--line);margin-right:4px;white-space:nowrap}
</style></head><body><h1 id="title">Common Room</h1><main>
<section><h2>Goal</h2><div id="goal" class="muted">loading</div></section>
<section><h2>Allowance</h2><div id="usage"></div></section>
<section class="wide"><h2>Working now</h2><div id="now" class="muted"></div><pre id="tail"></pre></section>
<section><h2>Your queue</h2><div id="queue"></div></section>
<section><h2>Suggest something</h2><form id="suggest"><input name="title" placeholder="One-line idea for the director to judge" required><button>Send</button></form>
<p class="muted" style="font-size:13px">Room channel: judged on its merits and may be rejected. Binding decisions belong in the executive session.</p>
<div style="margin-top:10px"><button id="pause">Pause autonomy</button> <span id="breakers"></span></div></section>
<section class="wide"><h2>Board</h2><div id="board"></div></section>
<section class="wide"><h2>Recent runs (spend vs output)</h2><table id="runs"></table></section>
</main><script>
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ago=t=>{const s=Math.max(0,Date.now()/1000-t);return s<90?Math.round(s)+'s':s<5400?Math.round(s/60)+'m':(s/3600).toFixed(1)+'h'};
const clock=t=>t?new Date(t*1000).toLocaleString([], {weekday:'short',hour:'2-digit',minute:'2-digit'}):'?';
async function refresh(){const r=await fetch('/api/state');const d=await r.json();
 $('title').textContent='Common Room: '+(d.project.name||'');
 const m=d.milestone;$('goal').innerHTML=m&&!m.error?`<b>${esc(d.goal)}</b><br>${m.closed_issues}/${m.open_issues+m.closed_issues} issues done${m.due_on?' · due '+m.due_on.slice(0,10):''}`:esc(d.goal||'no goal set');
 let u='';for(const [p,v] of Object.entries(d.usage)){u+=`<div><b>${p}</b> <span class="${v.allowed?'go':'hold'}">${v.allowed?'GO':'HOLD'}</span> <span class="muted" style="font-size:13px">${esc(v.reason)}</span></div>`;
  for(const w of ['five_hour','seven_day']){const x=v.windows[w];if(!x)continue;const cap=w=='seven_day'?v.detail.weekly_pace:v.detail.five_hour_cap;
   u+=`<div class="muted" style="font-size:12px">${w=='five_hour'?'5-hour':'weekly'} ${x.used_pct.toFixed(0)}% · resets ${clock(x.resets_at)} · read ${ago(x.observed_at)} ago</div><div class="bar"><i style="width:${Math.min(100,x.used_pct)}%"></i>${cap!=null?`<b style="left:${Math.min(100,cap)}%" title="pace limit"></b>`:''}</div>`}}
 $('usage').innerHTML=u;
 const run=d.running[0];$('now').innerHTML=run?`<b>${esc(run.role)}</b> on ${esc(run.task||'-')} with ${esc(run.provider)} ${esc(run.model||'')} · started ${ago(run.started_at)} ago`:'Nothing running.';
 $('tail').textContent=d.worker?`${d.worker.file} · ${d.worker.count} events · last update ${ago(d.worker.updated)} ago\\n`+d.worker.events.join('\\n'):'';
 renderQueue(d.owner_queue);
 $('board').innerHTML=(d.board||[]).map(i=>`<div>#${i.number} ${esc(i.title)} ${(i.labels||[]).filter(l=>/^(status|needs|owner):/.test(l)).map(l=>`<span class="tag">${esc(l)}</span>`).join('')}</div>`).join('')||'<span class="muted">empty</span>';
 $('runs').innerHTML=d.runs.map(r=>`<tr><td>${clock(r.started_at)}</td><td>${esc(r.role)}<br><span class="muted">${esc(r.provider)} ${esc(r.model||'')}</span></td><td>${esc(r.task||'')}</td><td class="${r.status=='done'?'go':r.status=='running'?'':'stop'}">${esc(r.status)}</td><td>${r.outputs.map(o=>esc(o.ref)).join('<br>')||'<span class="stop">no output</span>'}</td><td class="muted">${r.weekly_end!=null&&r.weekly_start!=null?'+'+(r.weekly_end-r.weekly_start).toFixed(1)+'% wk':''}${r.cost_usd?'<br>~$'+r.cost_usd.toFixed(2):''}</td></tr>`).join('');
 const paused=d.breakers.some(b=>b.kind=='paused_by_owner');$('pause').textContent=paused?'Resume autonomy':'Pause autonomy';$('pause').dataset.paused=paused?'1':'';
 $('breakers').innerHTML=d.breakers.map(b=>`<div class="stop" style="font-size:13px">${esc(b.kind)}: ${esc(b.detail)}</div>`).join('');}
const openItems=new Set();
function renderQueue(q){const el=$('queue');
 if(!q||q.error||!q.length){el.innerHTML=q&&q.error?`<span class="stop">${esc(q.error)}</span>`:'<span class="muted">Nothing assigned to you.</span>';return}
 if(el.contains(document.activeElement))return; // don't redraw while you're typing
 el.innerHTML=q.map(i=>`<details data-n="${i.number}" ${openItems.has(i.number)?'open':''} style="border-top:1px solid var(--line);padding:6px 0">
  <summary><b>#${i.number}</b> ${esc(i.title.replace(/^\\[You\\]\\s*/,''))} <span class="muted">due ${esc(i.due||'-')}</span>${i.comments.length&&i.comments[i.comments.length-1].who=='agent'?' <span class="tag">reply waiting</span>':''}</summary>
  <pre style="max-height:180px;margin:6px 0">${esc(i.brief)}</pre>
  ${i.comments.map(c=>`<div style="font-size:13px;margin:4px 0"><b class="${c.who=='you'?'':'go'}">${c.who=='you'?'You':'Room'}:</b> ${esc(c.body)}</div>`).join('')}
  <form data-n="${i.number}" style="margin-top:6px"><input name="text" placeholder="Answer, question, or what you delivered"><button name="act" value="send">Send</button><button name="act" value="done">Done</button></form>
  <a href="${esc(i.url)}" target="_blank" class="muted" style="font-size:12px">open on GitHub</a></details>`).join('');
 el.querySelectorAll('details').forEach(x=>x.ontoggle=()=>{const n=+x.dataset.n;x.open?openItems.add(n):openItems.delete(n)});
 el.querySelectorAll('form').forEach(f=>f.onsubmit=async e=>{e.preventDefault();const act=e.submitter?e.submitter.value:'send';let t=f.text.value.trim();
  if(act=='send'&&!t){f.text.placeholder='Write something first';return}
  if(act=='done')t='done'+(t?': '+t:'');
  const res=await post('/api/comment',{number:+f.dataset.n,text:t});if(res.error){alert(res.error);return}f.text.value='';refresh()})}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Room':'1'},body:JSON.stringify(body||{})});return r.json()}
$('pause').onclick=async()=>{await post($('pause').dataset.paused?'/api/resume':'/api/pause');refresh()};
$('suggest').onsubmit=async e=>{e.preventDefault();const t=e.target.title.value.trim();if(!t)return;const res=await post('/api/suggest',{title:t});e.target.title.value=res.url?'':t;alert(res.url?'Filed: '+res.url:'Failed: '+res.error)};
refresh();setInterval(refresh,10000);
</script></body></html>"""


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
                result = {"ok": True, "cleared": ledger.clear_breakers(project, PAUSE_KIND)}
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
                title = str(body.get("title", "")).strip()[:200]
                if not title:
                    raise ValueError("Write a one-line idea first")
                args = ["issue", "create", "--repo", self.cfg["project"]["repo"], "--title", title, "--label", "kind:suggestion",
                        "--body", "_Owner suggestion from the dashboard (room channel). The director triages it: accept, reject with reasons, or park._"]
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
