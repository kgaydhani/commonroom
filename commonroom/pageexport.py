"""`room page export FOLDER` (commonroom#647): the one page as static, read-only, noindex files.

The page's own html with its current state embedded, so it needs no local server and nothing from other sites. The
parts of the template between `//<write>` and `//</write>` (notes, share sliders, the link to the API) and the live
refresh between `//<live>` and `//</live>` are left out. Publishing the folder is the owner's own configured command.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from commonroom import dashboard

ROBOTS = "User-agent: *\nDisallow: /\n"
NOINDEX = '<meta name="robots" content="noindex, nofollow">'


def _without(html: str, tag: str) -> str:
    return re.sub(rf"[ \t]*//<{tag}>\n.*?//</{tag}>\n?", "", html, flags=re.S)


def build(data: dict, now: float) -> str:
    """The page as one html string: `data` embedded as JSON, built at `now`."""
    html = _without(_without(dashboard.PAGE, "write"), "live")
    html = html.replace('<meta name="viewport"', NOINDEX + '\n<meta name="viewport"', 1)
    embedded = json.dumps(data, default=str).replace("<", chr(92) + "u003c")   # a backslash-escape keeps data from closing the script
    run = (f'const BUILT = {json.dumps(now)};\n'
           '$("built").textContent = "Built " + when(BUILT) + ". A snapshot from the last beat, not live.";\n'
           f"render({embedded});\n")
    return html.replace("</script>", run + "</script>", 1)


def export(cfg: dict, folder: Path, now: float | None = None) -> list[Path]:
    """Write `index.html` and `robots.txt` into `folder` (made if missing); return the paths written."""
    now = time.time() if now is None else now
    folder.mkdir(parents=True, exist_ok=True)
    files = {"index.html": build(dashboard.page_data(cfg), now), "robots.txt": ROBOTS}
    out = []
    for name, text in files.items():
        path = folder / name
        path.write_text(text, encoding="utf-8", newline="\n")
        out.append(path)
    return out
