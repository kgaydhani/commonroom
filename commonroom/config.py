"""Project configuration: `room.toml` in the project repo, layered over defaults."""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS = {
    "project": {"name": None, "repo": None, "mission": ""},
    "goal": {"milestone": None},
    "budget": {
        # Claude is shared with the owner's own sessions, so it keeps reserves and is paced.
        "claude": {"reserve_weekly": 25, "reserve_5h": 10, "pace": True, "weekly_slack": 8, "front_load": 0.3},
        # Codex may be run dry by default.
        "codex": {"reserve_weekly": 0, "reserve_5h": 0, "pace": False, "weekly_slack": 0, "front_load": 1.0},
    },
    "freshness_seconds": 600,
    "breakers": {"no_output_runs": 3, "task_failures": 2},
    # A second Claude slot builds in parallel only when the queue is deep and weekly use is well under pace.
    "parallel": {"claude_slots": 2, "min_ready": 3, "min_pace_headroom": 15},
    "director_share_max": 0.15,
    "board": {"ready_cap": 15},
    "checks": {},
    "roles": {},
}


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def find_config(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / "room.toml"
        if candidate.is_file():
            return candidate
    return None


def load(path: Path | None = None) -> dict:
    """Return the merged config; `_path` and `_root` locate the project when one was found."""
    path = path or find_config()
    if path is None:
        return merge(DEFAULTS, {"_path": None, "_root": None})
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return merge(DEFAULTS, data) | {"_path": str(path), "_root": str(Path(path).parent)}


def budget(config: dict, provider: str) -> dict:
    return config["budget"].get(provider) or DEFAULTS["budget"]["claude"]
