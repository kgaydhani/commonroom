"""Read-only Reddit research through public archives (owner decision, 2026-10-06: "reddit yes", small reads only).

Reddit itself is closed to agents: Claude's WebFetch/WebSearch refuse it, and robots.txt plus its terms forbid
automated reads (gamemirage docs/scouting/2026-10-06-limitation-reddit.md). Arctic Shift and Pullpush are volunteer
archives that answer without an account. Rules: an honest user agent, at most one request every 5 s across every
process, a short-lived cache of query results only, no bulk collection, never used for training.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "commonroom-research/0.1 (read-only research; github.com/kgaydhani/commonroom)"
ARCTIC = "https://arctic-shift.photon-reddit.com/api"
PULLPUSH = "https://api.pullpush.io/reddit/search"
MIN_INTERVAL = 5.0       # seconds between any two requests, across all processes
CACHE_TTL = 24 * 3600    # query results only, for a day
MAX_LIMIT = 50


def home() -> Path:
    return Path(os.environ.get("COMMONROOM_HOME") or Path.home() / ".commonroom")


def _pace(now=time.time, sleep=time.sleep):
    stamp = home() / "reddit.last"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    try:
        wait = MIN_INTERVAL - (now() - float(stamp.read_text()))
    except (OSError, ValueError):
        wait = 0
    if wait > 0:
        sleep(wait)
    stamp.write_text(str(now()))


def _get(url: str) -> dict:
    _pace()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize(item: dict, source: str) -> dict:
    created = item.get("created_utc")
    text = item.get("selftext") or item.get("body") or ""
    link = item.get("permalink") or ""
    return {
        "title": item.get("title") or "(comment)",
        "subreddit": item.get("subreddit"),
        "date": time.strftime("%Y-%m-%d", time.gmtime(float(created))) if created else None,
        "score": item.get("score"),
        "comments": item.get("num_comments"),
        "excerpt": " ".join(text.split())[:300],
        "url": ("https://www.reddit.com" + link) if link.startswith("/") else link,
        "source": source,
    }


def search(query: str, subreddit: str | None = None, comments: bool = False, limit: int = 25,
           fetch=_get, cache_dir: Path | None = None, now: float | None = None) -> list[dict]:
    """Arctic Shift when a subreddit is given (its keyword search needs one), Pullpush otherwise or as the fallback."""
    limit = max(1, min(limit, MAX_LIMIT))
    now = time.time() if now is None else now
    cache_dir = cache_dir or home() / "cache" / "reddit"
    key = hashlib.sha256(json.dumps([query, subreddit, comments, limit]).encode()).hexdigest()[:20]
    cached = cache_dir / f"{key}.json"
    if cached.exists() and now - cached.stat().st_mtime < CACHE_TTL:
        return json.loads(cached.read_text(encoding="utf-8"))
    results, errors = None, []
    if subreddit:
        params = {"subreddit": subreddit, "limit": limit, "sort": "desc", ("body" if comments else "title"): query}
        try:
            data = fetch(f"{ARCTIC}/{'comments' if comments else 'posts'}/search?{urllib.parse.urlencode(params)}")
            if data.get("data") is None:
                raise RuntimeError(data.get("error") or "no data")
            results = [normalize(i, "arctic-shift") for i in data["data"]]
        except Exception as exc:
            errors.append(f"arctic-shift: {exc}")
    if results is None:
        params = {"q": query, "size": limit, **({"subreddit": subreddit} if subreddit else {})}
        try:
            data = fetch(f"{PULLPUSH}/{'comment' if comments else 'submission'}/?{urllib.parse.urlencode(params)}")
            results = [normalize(i, "pullpush") for i in data.get("data") or []]
        except Exception as exc:
            errors.append(f"pullpush: {exc}")
    if results is None:
        raise RuntimeError("; ".join(errors) or "no archive answered")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(results), encoding="utf-8")
    return results
