"""`room doctor`: read-only first-run checks. One line per check, `ok` or `missing` with the fix in one sentence.
It changes nothing, starts no model and writes nothing."""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from commonroom import config as config_mod, install, registry, usage, wire

MIN_PYTHON = (3, 11)


def run_gh(args: list[str]) -> int:
    """Exit code of `gh <args>`; 127 when gh cannot run."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def run_cmd(args: list[str]) -> tuple[int, str]:
    """(exit code, stdout) of a read-only command; (127, "") when it cannot run."""
    try:
        # schtasks writes in the console code page, not UTF-8: never fail on a byte we can't decode
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=30)
        return r.returncode, r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return 127, ""


CHECKOUT = Path(__file__).resolve().parents[1]


def _norm(text: str) -> str:
    return text.replace("\\", "/").replace('"', "").lower()


def beat_scheduled(checkout: Path, runner, windows: bool) -> tuple[str, str, list[str]]:
    """(status, why or note, heartbeat task names). Status is `ok`, `conflict` or `missing`.
    Windows: an enabled task whose action runs this checkout's beat.vbs/beat.cmd. An enabled per-project
    heartbeat.vbs/heartbeat.cmd task from this checkout stands in for it (ok with a note), and with a beat
    task as well it is a conflict, since both would start dispatchers. Elsewhere: a crontab line running
    `room.py beat` from this checkout."""
    root = _norm(str(checkout)).rstrip("/")
    if windows:
        code, text = runner(["schtasks", "/query", "/fo", "csv", "/v"])
        if code != 0:
            return "missing", "could not run `schtasks /query`", []
        rows = [r for r in csv.reader(io.StringIO(text)) if r]
        header = next((r for r in rows if "Task To Run" in r), None)
        if header is None:
            return "missing", "could not read `schtasks /query` output (expected English column names)", []
        col = lambda c: header.index(c) if c in header else None
        action, state, status, name = (col(c) for c in ("Task To Run", "Scheduled Task State", "Status", "TaskName"))
        beats: list[str] = []
        heartbeats: list[str] = []
        disabled = False
        for r in rows:
            if r is header or len(r) <= action or r[action] == "Task To Run":
                continue
            cmd = _norm(r[action])
            kind = beats if any(f"{root}/{b}" in cmd for b in ("beat.vbs", "beat.cmd")) else                 heartbeats if any(f"{root}/{b}" in cmd for b in ("heartbeat.vbs", "heartbeat.cmd")) else None
            if kind is None:
                continue
            flags = [r[i].strip().lower() for i in (state, status) if i is not None and i < len(r)]
            if "disabled" in flags:
                disabled = True
                continue
            kind.append(r[name].strip() if name is not None and name < len(r) else "(unnamed task)")
        if beats and heartbeats:
            return "conflict", "both a beat task and per-project heartbeat tasks would start dispatchers; disable " +                 ", ".join(f"`{h}`" for h in heartbeats), heartbeats
        if beats:
            return "ok", "", []
        if heartbeats:
            return "ok", "per-project heartbeat; beat.vbs replaces it (see room schedule)", heartbeats
        return "missing", "the scheduled beat task is disabled" if disabled else "no scheduled task runs this checkout's beat", []
    code, text = runner(["crontab", "-l"])
    if code != 0:
        return "missing", "could not run `crontab -l`", []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "room.py beat" in line and root in _norm(line):
            return "ok", "", []
    return "missing", "no crontab line runs `room.py beat` from this checkout", []


def agent_file_exists(root: Path, role: str) -> bool:
    """A role has a file `.claude/agents/<role>.md`, or one for its base name (`builder_heavy` uses `builder`)."""
    agents = root / ".claude" / "agents"
    return any((agents / f"{name}.md").is_file() for name in (role, role.split("_")[0]))


def codex_named(cfg: dict) -> bool:
    return any(isinstance(spec, list) and any(str(s).split(":")[0] == "codex" for s in spec)
               for spec in (cfg.get("roles") or {}).values())


def codex_reason(cfg: dict) -> str | None:
    """Why a project with a share uses Codex, or None when it opts out. The beat starts Codex slots unless
    `[parallel] codex_slots = 0`, and `codex_parallel_builds` (in `[roles]`) defaults to true."""
    if codex_named(cfg):
        return "a [roles] list names codex"
    try:
        slots = int((cfg.get("parallel") or {}).get("codex_slots", 2))
    except (TypeError, ValueError):
        slots = 2
    if slots != 0 and (cfg.get("roles") or {}).get("codex_parallel_builds", True):
        return "parallel builds use codex: set `codex_parallel_builds = false` in [roles] or `codex_slots = 0` in [parallel] to opt out"
    return None


def _json(path: Path):
    """(parsed object or None, problem or None) for a settings or hooks file."""
    if not path.is_file():
        return None, f"{path} does not exist"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} is unreadable: {exc}"
    return (data, None) if isinstance(data, dict) else (None, f"{path} is not a JSON object")


def install_checks(home: Path, claude: Path, codex: Path) -> list[tuple[str, str]]:
    """(name, problem) for each user-level install check; an empty problem means it passes. Reads only."""
    fix = "run `room install`"
    rows: list[tuple[str, str]] = []
    settings, bad = _json(claude / "settings.json")
    enabled = bool(settings and (settings.get("enabledPlugins") or {}).get(install.PLUGIN_KEY) is True)
    rows.append(("plugin enabled", "" if enabled else f"{bad or 'the plugin is not enabled in ' + str(claude / 'settings.json')}; {fix}"))
    market = ((settings or {}).get("extraKnownMarketplaces") or {}).get(install.MARKETPLACE) or {}
    src = (market.get("source") or {}).get("path") if isinstance(market, dict) else None
    runner = Path(src) / "plugin" / "hooks" / "run.py" if src else None
    if not src:
        problem = f"no `{install.MARKETPLACE}` marketplace in {claude / 'settings.json'}; {fix}"
    elif not runner.is_file():
        problem = f"{runner} does not exist; the checkout moved, run `room install` from it"
    else:
        problem = ""
    rows.append(("claude hooks", problem))
    hooks, bad = _json(codex / "hooks.json")
    problem = bad and f"{bad}; {fix}" or ""
    if hooks is not None:
        for event in wire.EVENTS:
            groups = (hooks.get("hooks") or {}).get(event)
            cmds = [h.get("command", "") for g in groups if isinstance(g, dict) for h in g.get("hooks", [])
                    if isinstance(h, dict)] if isinstance(groups, list) else []
            ours = [c for c in cmds if "plugin/hooks/run.py" in str(c)]
            if not ours:
                problem = f"hooks.{event} in {codex / 'hooks.json'} has no Common Room entry; {fix}"
                break
            m = re.search(r'"([^"]*plugin/hooks/run\.py)"', ours[0])
            if m and not Path(m.group(1)).is_file():
                problem = f"{m.group(1)} does not exist; run `room install` from the current checkout"
                break
    rows.append(("Codex hooks", problem))
    path_file = home / "room-path"
    if not path_file.is_file():
        problem = f"{path_file} is missing; {fix}"
    else:
        named = path_file.read_text(encoding="utf-8").strip()
        problem = "" if named and Path(named).is_file() else f"{path_file} names {named or 'nothing'}, which is not a room.py; {fix} from the current checkout"
    rows.append(("room-path", problem))
    return rows


def checks(reg_path: Path | str | None = None, which=shutil.which, gh=run_gh, version=None,
           runner=run_cmd, windows=None, checkout=None, home=None, claude=None, codex=None) -> list[tuple[bool, bool, str]]:
    """Each check as (ok, required, line text)."""
    out: list[tuple[bool, bool, str]] = []

    def add(ok: bool, name: str, fix: str = "", required: bool = True):
        out.append((ok, required, f"{name}: ok" if ok else f"{name}: missing - {fix}"))

    v = version or tuple(sys.version_info[:2])
    add(tuple(v) >= MIN_PYTHON, "python 3.11+", "install Python 3.11 or newer")
    add(bool(usage.claude_executable() if which is shutil.which else which("claude")), "claude on PATH",
        "install Claude Code and put `claude` on PATH")
    has_gh = bool(which("gh"))
    add(has_gh, "gh on PATH", "install the GitHub CLI and put `gh` on PATH")
    if has_gh:
        add(gh(["auth", "status"]) == 0, "gh logged in", "run `gh auth login`")

    win = sys.platform == "win32" if windows is None else windows
    where = checkout or CHECKOUT
    beat, why, _ = beat_scheduled(Path(where), runner, win)
    if beat == "ok":
        out.append((True, True, f"scheduled beat: ok ({why})" if why else "scheduled beat: ok"))
    elif beat == "conflict":
        out.append((False, True, f"scheduled beat: conflict - {why}"))
    else:
        fix = f"{why}; schedule `beat.vbs` (Windows Task Scheduler) or `room.py beat` (cron) for {where}"
        if win:
            fix += ", and disable any per-project heartbeat tasks first"
        add(False, "scheduled beat", fix)

    for name, problem in install_checks(Path(home) if home else install.room_home(),
                                        Path(claude) if claude else install.claude_dir(),
                                        Path(codex) if codex else wire.codex_dir()):
        add(not problem, name, problem)

    path = Path(reg_path) if reg_path else registry.default_path()
    reg = None
    try:
        reg = registry.load(path, check=False)
    except (registry.RegistryError, OSError, ValueError) as exc:
        add(False, "registry loads", f"fix {path}: {exc}")
    if reg is not None:
        found = registry.problems(reg)
        add(not found, "registry valid", "; ".join(found) + f" (edit {path} or use `room projects set`)")
        if not reg.projects:
            add(False, "registry has projects", "add one with `room projects add <name> <path> --share N`")
    codex: list[str] = []
    for name, project in sorted((reg.projects if reg else {}).items()):
        root = Path(project.path)
        label = f"project {name}"
        toml = root / "room.toml"
        if not root.is_dir():
            add(False, f"{label} path", f"{project.path} does not exist; fix it with `room projects add`")
            continue
        if not toml.is_file():
            add(False, f"{label} room.toml", f"add a room.toml to {root}")
            continue
        try:
            cfg = config_mod.load(toml)
        except Exception as exc:
            add(False, f"{label} room.toml", f"fix {toml}: {exc}")
            continue
        add(True, f"{label} room.toml")
        why = codex_reason(cfg) if project.share > 0 else None
        if why:
            codex.append(f"{name}: {why}")
        lacking = sorted(r for r, spec in (cfg.get("roles") or {}).items()
                         if isinstance(spec, list) and not agent_file_exists(root, r))
        add(not lacking, f"{label} roles", f"add {', '.join(f'.claude/agents/{r}.md' for r in lacking)} in {root}")
    if codex:
        add(bool(which("codex")), "codex on PATH", "install Codex and put `codex` on PATH (" + "; ".join(codex) + ")")
    return out


def run(reg_path=None, which=shutil.which, gh=run_gh, version=None, out=print,
        runner=run_cmd, windows=None, checkout=None, home=None, claude=None, codex=None) -> int:
    results = checks(reg_path, which, gh, version, runner, windows, checkout, home, claude, codex)
    for _, _, line in results:
        out(line)
    return 1 if any(not ok and required for ok, required, _ in results) else 0
