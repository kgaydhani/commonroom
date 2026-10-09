"""Read subscription meters without spending inference. An unknown reading is never free capacity.

Every reader returns observations: {provider, window, used_pct, resets_at, source}.
Window names are normalised to `five_hour` and `seven_day` where the duration is known.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
ROOT = Path(__file__).resolve().parent.parent


def observation(provider: str, window: str, used_pct, resets_at, source: str) -> dict:
    used, reset = float(used_pct), (float(resets_at) if resets_at is not None else None)
    if not math.isfinite(used) or not -0.001 <= used <= 1000:
        raise ValueError(f"Invalid usage percentage for {provider}/{window}: {used_pct!r}")
    if reset is not None and (not math.isfinite(reset) or reset <= 0):
        raise ValueError(f"Invalid reset time for {provider}/{window}: {resets_at!r}")
    return {"provider": provider, "window": window, "used_pct": max(0.0, used), "resets_at": reset, "source": source}


def clean_env() -> dict:
    """Never let an inherited API key silently move subscription work onto API billing."""
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "CLAUDECODE"):
        env.pop(key, None)
    return env


# Claude ---------------------------------------------------------------------
def from_statusline(data: dict) -> list[dict]:
    """Claude Code passes `rate_limits` to status-line commands for Pro/Max subscribers."""
    limits = data.get("rate_limits") or {}
    result = []
    for name, window in limits.items():
        if isinstance(window, dict) and window.get("used_percentage") is not None:
            result.append(observation("claude", name, window["used_percentage"], window.get("resets_at"), "statusline"))
    return result


def from_claude_event(event: dict) -> list[dict]:
    """`rate_limit_event` lines from `claude -p --output-format stream-json`."""
    if event.get("type") != "rate_limit_event":
        return []
    info = event.get("rate_limit_info") or {}
    result = []
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        for name, w in windows.items():
            if isinstance(w, dict) and w.get("utilization") is not None:
                result.append(observation("claude", name, float(w["utilization"]) * 100, w.get("resetsAt"), "stream"))
    elif info.get("rateLimitType") and info.get("utilization") is not None:
        result.append(observation("claude", info["rateLimitType"], float(info["utilization"]) * 100,
                                  info.get("resetsAt"), "stream"))
    return result


def claude_overage(event: dict) -> bool:
    info = event.get("rate_limit_info") or {}
    return bool(info.get("isUsingOverage"))


def _ui_reset(text: str, now: float) -> float:
    """Claude's /usage screen shows minute precision, e.g. 'Resets 2pm (Europe/London)'."""
    match = re.fullmatch(r"Resets (.+?) \(([^)]+)\)", text.strip())
    if not match:
        raise ValueError("Unrecognized Claude reset label")
    label, tz = match.groups()
    local = datetime.fromtimestamp(now, ZoneInfo(tz))
    label = label.replace(" at ", " ").replace(",", "")
    for fmt in ("%I:%M%p", "%I%p", "%b %d %I:%M%p", "%b %d %I%p"):
        try:
            parsed = datetime.strptime(f"{label} {local.year}" if "%b" in fmt else label,
                                       f"{fmt} %Y" if "%b" in fmt else fmt)
        except ValueError:
            continue
        if "%b" not in fmt:
            result = local.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            if result.timestamp() <= now:
                result += timedelta(days=1)
        else:
            result = parsed.replace(tzinfo=local.tzinfo)
            if result.timestamp() <= now:
                result = result.replace(year=result.year + 1)
        return result.timestamp()
    raise ValueError("Claude reset format changed")


LIMIT_PATTERNS = (r"hit your usage limit", r"usage limit reached", r"hit your limit", r"(5-hour|weekly|session) limit reached")


def _clock_time(label: str, now: float, tz: str | None = None) -> float | None:
    """'8:31 PM' or '10pm' -> the next such time after `now`, in `tz` or the machine's local zone."""
    label = label.strip().upper().replace(" ", "")
    local = datetime.fromtimestamp(now, ZoneInfo(tz)) if tz else datetime.fromtimestamp(now).astimezone()
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            parsed = datetime.strptime(label, fmt)
        except ValueError:
            continue
        result = local.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        if result.timestamp() <= now:
            result += timedelta(days=1)
        return result.timestamp()
    return None


def limit_hit(texts: list[str], now: float, fallback: float = 1800) -> float | None:
    """If a worker stopped because its provider's usage limit was hit, when to try again (epoch seconds).

    Codex: "You've hit your usage limit ... try again at 8:31 PM."  Claude: "Claude AI usage limit reached|<epoch>"
    or "You've hit your limit · resets 10pm (Europe/London)". None means this was not a usage-limit stop."""
    text = " ".join(t for t in texts if t).replace("\\u2019", "'").replace("’", "'")
    if not any(re.search(p, text, re.I) for p in LIMIT_PATTERNS):
        return None
    epoch = re.search(r"limit reached\|(\d{10})", text, re.I)
    if epoch:
        return float(epoch[1])
    clock = re.search(r"(?:try again at|resets(?: at)?)\s+(\d{1,2}(?::\d{2})?\s?[ap]m)(?:\s*\(([\w/+-]+)\))?", text, re.I)
    if clock:
        try:
            when = _clock_time(clock[1], now, clock[2])
        except Exception:
            when = None
        if when:
            return when
    return now + fallback


_owner_cache: dict = {}


def session_folder(path: str | Path) -> str:
    """Claude Code's folder name under ~/.claude/projects for sessions started in `path`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path)))


def owner_active(project_root: str | None, minutes: float = 20, projects_dir: Path | None = None,
                 now: float | None = None, exclude: list[str] | tuple = ()) -> str | None:
    """Is the owner using Claude Code elsewhere on this machine? Looks for session transcripts under
    ~/.claude/projects modified in the last `minutes`, outside this project (its director chat and room workers
    all live under the project's own encoded folder name) and outside `exclude`: the folders where room workers of
    any project ran (another project's workers are the room, not the owner). Returns that folder's name, or None.
    Cached for a minute. Usage on claude.ai or other devices can't be seen here; the plan-wide meter still counts it."""
    now = time.time() if now is None else now
    key = (project_root, minutes, str(projects_dir), tuple(sorted(exclude)))
    hit = _owner_cache.get(key)
    if hit and now - hit[0] < 60:
        return hit[1]
    root = projects_dir or Path.home() / ".claude" / "projects"
    mine = session_folder(project_root) if project_root else None
    room = tuple(session_folder(p) for p in exclude)
    found = None
    try:
        for folder in root.iterdir():
            if not folder.is_dir() or (mine and folder.name.startswith(mine)) or (room and folder.name.startswith(room)):
                continue
            if any(now - f.stat().st_mtime < minutes * 60 for f in folder.glob("*.jsonl")):
                found = folder.name
                break
    except OSError:
        pass
    _owner_cache[key] = (now, found)
    return found


def parse_claude_screen(text: str, now: float) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result, current = [], None
    for line in lines:
        if line == "Current session" or line.startswith("Current week"):
            current = {"name": line}
        elif current and re.search(r"(\d+(?:\.\d+)?)% used", line):
            current["used"] = float(re.search(r"(\d+(?:\.\d+)?)% used", line)[1])
        elif current and line.startswith("Resets ") and "used" in current:
            name = current["name"]
            window = ("five_hour" if name == "Current session" else "seven_day" if "all models" in name
                      else "seven_day_" + re.sub(r"\W+", "_", name.split("(")[-1].strip(") ").lower()))
            result.append(observation("claude", window, current["used"], _ui_reset(line, now), "usage-screen"))
            current = None
    return result


def claude_executable() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path.home() / ".local/bin/claude.exe"
    return str(candidate) if candidate.is_file() else None


def claude_screen_limits(cwd: str | None = None, timeout: float = 45) -> list[dict]:
    """Fallback: open Claude's built-in /usage panel in a hidden terminal. Needs `pywinpty` and `pyte`."""
    if os.name != "nt":
        raise RuntimeError("The /usage screen reader currently supports Windows only")
    try:
        from winpty import PtyProcess  # type: ignore
        import pyte  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install the optional 'pty' extras (pywinpty, pyte) to use the /usage screen reader") from exc
    binary = claude_executable()
    if not binary:
        raise RuntimeError("Claude CLI not found")
    screen, chunks = pyte.Screen(160, 60), queue.Queue()
    stream = pyte.Stream(screen)
    cwd = cwd or os.environ.get("COMMONROOM_PROBE_DIR") or str(ROOT)
    process = PtyProcess.spawn([binary, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--tools", "",
                                "--no-chrome", "--permission-mode", "dontAsk"],
                               cwd=cwd, env=clean_env(),
                               dimensions=(60, 160))

    def read():
        try:
            while process.isalive():
                chunks.put(process.read(65536))
        except (EOFError, OSError):
            pass

    threading.Thread(target=read, daemon=True).start()
    sent = entered = False
    ready_at = enter_at = None
    launched = time.monotonic()
    text = ""
    try:
        while time.monotonic() < launched + timeout:
            try:
                stream.feed(chunks.get(timeout=.2))
            except queue.Empty:
                pass
            text = "\n".join(screen.display)
            if "Yes, I trust this folder" in text:
                raise RuntimeError(f"Open Claude once in {cwd} and trust the folder to enable the /usage reader")
            if "Claude Code v" in text and ready_at is None:
                ready_at = max(time.monotonic() + 2, launched + 10)
            if ready_at and time.monotonic() >= ready_at and not sent:
                process.write("/usage"); sent = True; enter_at = time.monotonic() + 2
            if sent and not entered and time.monotonic() >= enter_at:
                process.write("\r"); entered = True
            if entered and any(label in text for label in ("Extra usage", "Usage credits")):
                found = parse_claude_screen(text, time.time())
                if {"five_hour", "seven_day"} <= {o["window"] for o in found}:
                    return found
        raise RuntimeError("Claude /usage panel did not render a complete reading")
    finally:
        process.terminate(force=True)


# Codex ----------------------------------------------------------------------
def codex_executable() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "OpenAI/Codex/bin"
    candidates = [p for p in base.glob("*/codex.exe") if p.is_file()]
    return str(max(candidates, key=lambda p: p.stat().st_mtime)) if candidates else None


def normalize_codex(data: dict) -> list[dict]:
    buckets = data.get("rateLimitsByLimitId") or {"codex": data.get("rateLimits") or {}}
    result = []
    for key, bucket in buckets.items():
        for label in ("primary", "secondary"):
            w = bucket.get(label)
            if not w:
                continue
            minutes = w.get("windowDurationMins")
            window = {300: "five_hour", 10080: "seven_day"}.get(minutes, f"{key}/{label}")
            if key != "codex" and window in WINDOW_SECONDS:
                window = f"{window}:{key}"
            result.append(observation("codex", window, w["usedPercent"], w.get("resetsAt"), "app-server"))
    return result


def codex_limits(timeout: float = 20) -> list[dict]:
    binary = codex_executable()
    if not binary:
        raise RuntimeError("Codex CLI not found")
    process = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=clean_env(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    replies = queue.Queue()

    def read():
        with process.stdout:
            for line in process.stdout:
                try:
                    replies.put(json.loads(line))
                except ValueError:
                    pass

    threading.Thread(target=read, daemon=True).start()

    def rpc(i, method, params=None):
        process.stdin.write(json.dumps({"id": i, "method": method, **({"params": params} if params else {})}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = replies.get(timeout=max(.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if reply.get("id") == i:
                if "error" in reply:
                    raise RuntimeError("Codex quota read failed; check `codex login`")
                return reply["result"]
        raise TimeoutError("Codex quota read timed out")

    try:
        rpc(1, "initialize", {"clientInfo": {"name": "commonroom", "version": "2.0"}})
        process.stdin.write('{"method":"initialized"}\n'); process.stdin.flush()
        return normalize_codex(rpc(2, "account/rateLimits/read"))
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
