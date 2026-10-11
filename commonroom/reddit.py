"""Read-only Reddit research through public archives (owner decision, 2026-10-06: "reddit yes", small reads only).

Reddit itself is closed to agents: Claude's WebFetch/WebSearch refuse it, and robots.txt plus its terms forbid
automated reads (gamemirage docs/scouting/2026-10-06-limitation-reddit.md). Arctic Shift is a volunteer archive that
answers without an account. Pullpush is not used: it answers agents with 429 "This website does not provide free scraping
resources for agents", and we take it at its word (commonroom#38). Rules: an honest user agent, at most one request every 5 s across every
process, a short-lived cache of query results only, no bulk collection, never used for training.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "commonroom-research/0.1 (read-only research; github.com/kgaydhani/commonroom)"
ARCTIC = "https://arctic-shift.photon-reddit.com/api"
MIN_INTERVAL = 5.0       # seconds between any two requests, across all processes
CACHE_TTL = 24 * 3600    # query results only, for a day
MAX_LIMIT = 50
RETRY_WAIT = 10.0        # Arctic Shift answers a slow full-text search with 422 "Timeout. Maybe slow down a bit"
NEEDS_SUB = ("a subreddit is required: Pullpush refuses agents and Arctic Shift's keyword search needs a subreddit, "
             "so pass --sub <name>")


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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The archives say why in the body. Without it a 422 read as "HTTP Error 422: Unprocessable Entity" and was
        # blamed on the wrong parameter; the body said "Timeout. Maybe slow down a bit" (commonroom#26).
        body = exc.read().decode("utf-8", "replace")
        try:
            reason = json.loads(body).get("error") or body
        except (ValueError, AttributeError):
            reason = body
        raise RuntimeError(f"HTTP {exc.code}: {' '.join(str(reason).split())[:300] or exc.reason}") from None


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
           fetch=_get, cache_dir: Path | None = None, now: float | None = None, sleep=time.sleep) -> list[dict]:
    """Arctic Shift only; its keyword search needs a subreddit, so none raises ValueError(NEEDS_SUB) before any request.
    Arctic Shift searches `title` (posts) or `body` (comments); it rejects `query`."""
    if not subreddit:
        raise ValueError(NEEDS_SUB)
    limit = max(1, min(limit, MAX_LIMIT))
    now = time.time() if now is None else now
    cache_dir = cache_dir or home() / "cache" / "reddit"
    key = hashlib.sha256(json.dumps([query, subreddit, comments, limit]).encode()).hexdigest()[:20]
    cached = cache_dir / f"{key}.json"
    if cached.exists() and now - cached.stat().st_mtime < CACHE_TTL:
        return json.loads(cached.read_text(encoding="utf-8"))
    # No `sort`: results already come newest first, and `sort=desc` made the same title search of r/ClaudeCode
    # time out where the unsorted one answered (2026-10-09).
    params = {"subreddit": subreddit, "limit": limit, ("body" if comments else "title"): query}
    url = f"{ARCTIC}/{'comments' if comments else 'posts'}/search?{urllib.parse.urlencode(params)}"
    for attempt in (1, 2):
        try:
            data = fetch(url)
            if data.get("data") is None:
                raise RuntimeError(data.get("error") or "no data")
            results = [normalize(i, "arctic-shift") for i in data["data"]]
            break
        except Exception as exc:
            if attempt == 1 and re.search(r"time(d)? ?out", str(exc), re.I):
                sleep(RETRY_WAIT)   # a timeout is the archive under load: back off and ask once more
                continue
            raise RuntimeError(f"arctic-shift: {exc}") from None
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(results), encoding="utf-8")
    return results
