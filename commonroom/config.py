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
    # While the owner uses Claude elsewhere on this machine, extra slots yield and no new work starts above this
    # share of the 5-hour window, so the owner's own sessions keep room to breathe.
    "owner": {"active_minutes": 20, "new_work_cap_5h": 70},
    # Reasoning effort per role (`claude --effort`). Unlisted roles use the CLI's default. Recorded on every run.
    "effort": {"director": "high", "auditor": "high"},
    "director_share_max": 0.15,
    # The headless director also steers on a cadence, with no other trigger: every `steer_every_hours` since its
    # last run (0 turns it off). commonroom#41.
    "director": {"steer_every_hours": 3},
    "board": {"ready_cap": 15},
    # `test` is the project's test command. `ci = false` declares the repo has no GitHub CI: housekeeping then merges on
    # a reviewer's approval that states a passing run of `test` on the PR head (dispatch.tested_on_head).
    "checks": {},
    "roles": {},
    # A guest project rides on a host project's plan (commonroom#31). `host` is the host's folder: the guest uses the
    # host's budget gate and is held by the host's room-wide stops. `max` caps the guest's share of autonomous run time
    # per provider over the trailing `hours`, unless no other project has used that provider for `idle_minutes`.
    # Empty: the project is not a guest and has the whole allowance (unchanged behaviour).
    "share": {},
}

# What a guest takes from its host, so both pass through one budget gate.
GATE_KEYS = ("budget", "owner", "freshness_seconds")


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


def load(path: Path | None = None, follow_host: bool = True) -> dict:
    """Return the merged config; `_path` and `_root` locate the project when one was found. A guest (`[share] host`)
    takes the host's budget gate settings, and `_host` names the host project."""
    path = path or find_config()
    if path is None:
        return merge(DEFAULTS, {"_path": None, "_root": None})
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    cfg = merge(DEFAULTS, data) | {"_path": str(path), "_root": str(Path(path).parent)}
    host = (cfg.get("share") or {}).get("host")
    if host and follow_host:
        host_path = Path(host) if str(host).endswith(".toml") else Path(host) / "room.toml"
        if host_path.is_file():
            host_cfg = load(host_path, follow_host=False)
            for key in GATE_KEYS:
                cfg[key] = copy.deepcopy(host_cfg[key])
            cfg["_host"] = host_cfg["project"].get("name")
    return cfg


def has_ci(cfg: dict) -> bool:
    """False only when the project declares `[checks] ci = false`."""
    return bool((cfg.get("checks") or {}).get("ci", True))


def budget(config: dict, provider: str) -> dict:
    return config["budget"].get(provider) or DEFAULTS["budget"]["claude"]
