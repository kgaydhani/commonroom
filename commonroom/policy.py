"""The room's ways-of-working numbers (commonroom#217): one table, commonroom/ways/policy.toml, read once at import.

A running dispatcher keeps the values it loaded while the room pulls new files under it, and a missing file or name
fails the import naming it. A project overrides a value under `[policy]` in its room.toml; `overrides` checks the
names and returns only what the project set, so an unset name keeps the room's value. A value with a floor or ceiling
is written `name = { value = 2, min = 1 }` in the table; a project's value, and the room's own, must sit inside it
(commonroom#230)."""
from __future__ import annotations

import tomllib
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parent / "ways" / "policy.toml"
NAMES = ("review_rounds", "incomplete_rounds", "ci_bounces", "ci_reruns", "ci_backoff_minutes",
         "scout_interleave_seconds", "director_cooldown_seconds", "stamp_cooldown_seconds", "owner_cooldown_seconds", "stale_claim_minutes",
         "room_error_backoff_seconds", "room_error_escalate", "resume_window_seconds", "rework_resume_seconds", "progress_min_work_seconds",
         "notify_max", "heartbeat_failures", "run_timeout_seconds", "prompt_max_tokens", "templates_max_tokens", "pie_idle_minutes",
         "owner_floor_weekly", "pie_notice_margin", "pie_own_active_minutes", "cold_return_minutes", "cold_return_context",
         "handoff_offer_hours", "looking_max_percent", "unread_note_hours", "person_max_lines", "person_max_tokens", "person_stale_days",
         "places_listed", "places_days", "session_start_max_tokens")
# Name -> why no project may set it.
GLOBAL_ONLY = {"prompt_max_tokens": "because it limits the room's own text (axiom A9)",
               "templates_max_tokens": "because it limits the room's own text (axiom A9)",
               "progress_min_work_seconds": "because it is part of the progress breaker's timing, which is the "
                                            "director's call (commonroom#230)",
               "pie_idle_minutes": "because every project's dispatcher must judge who is active alike (commonroom#240)",
               "pie_notice_margin": "because it is the owner's own notice setting, the same for every project (commonroom#243)",
               "pie_own_active_minutes": "because every project's dispatcher must judge the owner's activity alike "
                                         "(commonroom#243)",
               "owner_floor_weekly": "because it keeps the owner's own use first against every project alike "
                                     "(axiom A5, commonroom#241)",
               "cold_return_minutes": "because it shapes the owner's own sessions alike in every project "
                                      "(commonroom#367)",
               "cold_return_context": "because it shapes the owner's own sessions alike in every project "
                                      "(commonroom#367)",
               "handoff_offer_hours": "because it shapes the owner's own sessions alike in every project "
                                      "(commonroom#384)",
               "person_max_lines": "because it caps the owner's own person file, the same in every project (commonroom#573)",
               "person_max_tokens": "because it caps the owner's own person file, the same in every project (commonroom#573)",
               "person_stale_days": "because it ages the owner's own person file, the same in every project (commonroom#574)",
               "places_listed": "because it shapes the owner's own sessions alike in every project (commonroom#578)",
               "places_days": "because it shapes the owner's own sessions alike in every project (commonroom#578)",
               "session_start_max_tokens": "because it limits the room's own text (axiom A9, commonroom#578)"}


def check(name: str, value, where: str, low: int = 0, high: int | None = None) -> int:
    if (isinstance(value, bool) or not isinstance(value, int) or value < low
            or (high is not None and value > high)):
        span = f"of at least {low}" if high is None else f"between {low} and {high}"
        raise ValueError(f"{where}: {name} must be a whole number {span}, not {value!r}")
    return value


def read_table(path: Path) -> tuple[dict[str, int], dict[str, tuple[int, int | None]]]:
    """Every value in the table at `path`, and each name's (floor, ceiling): (0, None) when it sets neither. A missing
    file raises FileNotFoundError naming it; a missing, unknown or non-integer name, or a value outside its own
    bounds, raises ValueError naming it."""
    with open(path, "rb") as handle:
        table = tomllib.load(handle)
    missing = [n for n in NAMES if n not in table]
    unknown = sorted(set(table) - set(NAMES))
    if missing or unknown:
        raise ValueError(f"{path}: " + "; ".join(([f"missing {', '.join(missing)}"] if missing else [])
                                                 + ([f"unknown {', '.join(unknown)}"] if unknown else [])))
    values, bounds = {}, {}
    for n in NAMES:
        entry = table[n] if isinstance(table[n], dict) else {"value": table[n]}
        stray = sorted(set(entry) - {"value", "min", "max"})
        if stray or "value" not in entry:
            raise ValueError(f"{path}: {n} takes value, min and max" + (f", not {', '.join(stray)}" if stray else ""))
        low = check(f"{n} min", entry.get("min", 0), str(path))
        high = None if "max" not in entry else check(f"{n} max", entry["max"], str(path), low)
        values[n], bounds[n] = check(n, entry["value"], str(path), low, high), (low, high)
    return values, bounds


def load_policy(path: Path) -> dict[str, int]:
    """Every value in the table at `path` (see `read_table`)."""
    return read_table(path)[0]


def overrides(cfg: dict) -> dict[str, int]:
    """The values a project's room.toml `[policy]` sets, checked: an unknown name, one only the room sets, or a value
    outside the table's floor and ceiling raises ValueError naming it. Empty when the project sets none."""
    table = cfg.get("policy") or {}
    if not isinstance(table, dict):
        raise ValueError("room.toml [policy] must be a table")
    for name, value in table.items():
        if name not in DEFAULTS:
            raise ValueError(f"room.toml [policy] has unknown name {name!r}; known: {', '.join(NAMES)}")
        if name in GLOBAL_ONLY:
            raise ValueError(f"room.toml [policy] {name} is room-wide only {GLOBAL_ONLY[name]}")
        check(name, value, "room.toml [policy]", *BOUNDS[name])
    return dict(table)


DEFAULTS, BOUNDS = read_table(POLICY_FILE)
