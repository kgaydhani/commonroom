"""The room's ways-of-working numbers (commonroom#217): one table, commonroom/ways/policy.toml, read once at import.

A running dispatcher keeps the values it loaded while the room pulls new files under it, and a missing file or name
fails the import naming it. A project overrides a value under `[policy]` in its room.toml; `overrides` checks the
names and returns only what the project set, so an unset name keeps the room's value."""
from __future__ import annotations

import tomllib
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parent / "ways" / "policy.toml"
NAMES = ("review_rounds", "incomplete_rounds", "ci_bounces", "ci_reruns", "ci_backoff_minutes",
         "scout_interleave_seconds", "director_cooldown_seconds", "owner_cooldown_seconds", "stale_claim_minutes",
         "room_error_backoff_seconds", "room_error_escalate", "resume_window_seconds", "progress_min_work_seconds",
         "notify_max", "run_timeout_seconds", "prompt_max_tokens", "templates_max_tokens")
# Name -> why no project may set it.
GLOBAL_ONLY = {"ci_reruns": "for now: GitHubBoard.rerun_ci reads it, and no project's value reaches it (commonroom#228)",
               "ci_backoff_minutes": "for now: GitHubBoard.rerun_ci reads it, and no project's value reaches it "
                                     "(commonroom#228)",
               "prompt_max_tokens": "because it limits the room's own text (axiom A9)",
               "templates_max_tokens": "because it limits the room's own text (axiom A9)"}


def check(name: str, value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where}: {name} must be a whole number of at least 0, not {value!r}")
    return value


def load_policy(path: Path) -> dict[str, int]:
    """Every value in the table at `path`. A missing file raises FileNotFoundError naming it; a missing, unknown or
    non-integer name raises ValueError naming it."""
    with open(path, "rb") as handle:
        table = tomllib.load(handle)
    missing = [n for n in NAMES if n not in table]
    unknown = sorted(set(table) - set(NAMES))
    if missing or unknown:
        raise ValueError(f"{path}: " + "; ".join(([f"missing {', '.join(missing)}"] if missing else [])
                                                 + ([f"unknown {', '.join(unknown)}"] if unknown else [])))
    return {n: check(n, table[n], str(path)) for n in NAMES}


def overrides(cfg: dict) -> dict[str, int]:
    """The values a project's room.toml `[policy]` sets, checked: an unknown name, or one only the room sets, raises
    ValueError naming it. Empty when the project sets none."""
    table = cfg.get("policy") or {}
    if not isinstance(table, dict):
        raise ValueError("room.toml [policy] must be a table")
    for name, value in table.items():
        if name not in DEFAULTS:
            raise ValueError(f"room.toml [policy] has unknown name {name!r}; known: {', '.join(NAMES)}")
        if name in GLOBAL_ONLY:
            raise ValueError(f"room.toml [policy] {name} is room-wide only {GLOBAL_ONLY[name]}")
        check(name, value, "room.toml [policy]")
    return dict(table)


DEFAULTS = load_policy(POLICY_FILE)
