"""`room init PATH`: write a starter room.toml and agent files from the templates in `ways/templates/` (commonroom#500).
It never overwrites, starts no model and does not register the project."""
from __future__ import annotations

import json
from pathlib import Path

TEMPLATES = Path(__file__).parent / "ways" / "templates"
AGENTS = ("builder", "reviewer", "director")
DEFAULT_GOAL = "First milestone"


def targets(root: Path) -> list[Path]:
    return [root / "room.toml", *(root / ".claude" / "agents" / f"{role}.md" for role in AGENTS)]


def render(root: Path, goal: str) -> dict[Path, str]:
    """The text of each file to write. Name and goal are filled in as TOML strings."""
    toml = (TEMPLATES / "room.toml").read_text(encoding="utf-8")
    toml = toml.replace("{name}", json.dumps(root.name)).replace("{goal}", json.dumps(goal))
    files = {root / "room.toml": toml}
    for role in AGENTS:
        files[root / ".claude" / "agents" / f"{role}.md"] = (TEMPLATES / f"{role}.md").read_text(encoding="utf-8")
    return files


def next_step(root: Path) -> str:
    return f'room projects add {root.name} "{root}" --share 10'


def run(path: str, goal: str | None = None, out=print, err=None) -> int:
    root = Path(path).resolve()
    err = err or out
    if not root.is_dir():
        err(f"refused: {root} is not a folder")
        return 1
    existing = [t for t in targets(root) if t.exists()]
    if existing:
        err("refused: nothing written, these already exist:")
        for t in existing:
            err(f"  {t}")
        return 1
    for target, text in render(root, goal or DEFAULT_GOAL).items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        out(f"wrote {target}")
    out("edit [project] repo in room.toml, then register the project (set --share to its slice of the allowance):")
    out(f"  {next_step(root)}")
    return 0
