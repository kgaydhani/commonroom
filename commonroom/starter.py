"""`room init PATH`: write a starter room.toml and agent files from the templates in `ways/templates/` (commonroom#500).
It never overwrites, starts no model and does not register the project."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

TEMPLATES = Path(__file__).parent / "ways" / "templates"
AGENTS = ("builder", "reviewer", "director")
KINDS = {"code": ("", AGENTS), "research": ("research", ("researcher", "director"))}   # kind -> (template folder, agent files)
DEFAULT_GOAL = "First milestone"


def project_name(root: Path) -> str:
    """The folder name with every character the registry refuses replaced by `-`."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", root.name) or "project"


def targets(root: Path, kind: str = "code") -> list[Path]:
    return [root / "room.toml", *(root / ".claude" / "agents" / f"{role}.md" for role in KINDS[kind][1])]


def render(root: Path, goal: str, kind: str = "code") -> dict[Path, str]:
    """The text of each file to write. Name and goal are filled in as TOML strings."""
    folder, agents = KINDS[kind]
    templates = TEMPLATES / folder
    toml = (templates / "room.toml").read_text(encoding="utf-8")
    # ensure_ascii=False: json's surrogate-pair escapes for characters above U+FFFF are not valid TOML.
    toml = (toml.replace("{name}", json.dumps(project_name(root), ensure_ascii=False))
            .replace("{goal}", json.dumps(goal, ensure_ascii=False)))
    files = {root / "room.toml": toml}
    for role in agents:
        files[root / ".claude" / "agents" / f"{role}.md"] = (templates / f"{role}.md").read_text(encoding="utf-8")
    return files


def next_step(root: Path) -> str:
    quote = subprocess.list2cmdline if sys.platform == "win32" else shlex.join
    return "room projects add " + quote([project_name(root), str(root)]) + " --share 10"


def run(path: str, goal: str | None = None, out=print, err=None, kind: str = "code") -> int:
    root = Path(path).resolve()
    err = err or out
    if not root.is_dir():
        err(f"refused: {root} is not a folder")
        return 1
    if kind not in KINDS:
        err(f"refused: unknown --kind {kind!r}; one of {', '.join(KINDS)}")
        return 1
    existing = [t for t in targets(root, kind) if t.exists()]
    if existing:
        err("refused: nothing written, these already exist:")
        for t in existing:
            err(f"  {t}")
        return 1
    files = render(root, goal or DEFAULT_GOAL, kind)
    try:
        tomllib.loads(files[root / "room.toml"])
    except tomllib.TOMLDecodeError as exc:
        err(f"refused: nothing written, the goal makes room.toml unreadable ({exc}); pick another --goal")
        return 1
    for target, text in files.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        out(f"wrote {target}")
    out("edit [project] mission in room.toml, then" if kind == "research" else "edit [project] repo in room.toml, then register the project (set --share to its slice of the allowance):")
    out(f"  {next_step(root)}")
    return 0
