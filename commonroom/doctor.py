"""`room doctor`: read-only first-run checks. One line per check, `ok` or `missing` with the fix in one sentence.
It changes nothing, starts no model and writes nothing."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from commonroom import config as config_mod, registry, usage

MIN_PYTHON = (3, 11)


def run_gh(args: list[str]) -> int:
    """Exit code of `gh <args>`; 127 when gh cannot run."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def agent_file_exists(root: Path, role: str) -> bool:
    """A role has a file `.claude/agents/<role>.md`, or one for its base name (`builder_heavy` uses `builder`)."""
    agents = root / ".claude" / "agents"
    return any((agents / f"{name}.md").is_file() for name in (role, role.split("_")[0]))


def codex_named(cfg: dict) -> bool:
    return any(isinstance(spec, list) and any(str(s).split(":")[0] == "codex" for s in spec)
               for spec in (cfg.get("roles") or {}).values())


def checks(reg_path: Path | str | None = None, which=shutil.which, gh=run_gh, version=None) -> list[tuple[bool, bool, str]]:
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
    codex = False
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
        codex = codex or codex_named(cfg)
        lacking = sorted(r for r, spec in (cfg.get("roles") or {}).items()
                         if isinstance(spec, list) and not agent_file_exists(root, r))
        add(not lacking, f"{label} roles", f"add {', '.join(f'.claude/agents/{r}.md' for r in lacking)} in {root}")
    if codex:
        add(bool(which("codex")), "codex on PATH", "install Codex and put `codex` on PATH (a project's roles name it)")
    return out


def run(reg_path=None, which=shutil.which, gh=run_gh, version=None, out=print) -> int:
    results = checks(reg_path, which, gh, version)
    for _, _, line in results:
        out(line)
    return 1 if any(not ok and required for ok, required, _ in results) else 0
