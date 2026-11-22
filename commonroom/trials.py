"""Ways-of-working trials: the declaration and the arm assignment (commonroom#350). Pure code: no GitHub calls, no
clock reads (`today` is a parameter), and it imports neither `dispatch` nor `policy`; callers pass the prompt names
and policy bounds."""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from commonroom import metrics
from commonroom.waytext import PLACEHOLDER, read_way

FILE = Path(__file__).resolve().parent / "ways" / "trials.toml"   # the declared trials (the dispatcher reads this too)
MEASURE_FIELD = {"review_runs_per_landing": "review_runs_per_merge", "minutes_per_landing": "minutes_per_merge",
                 "input_tokens_per_landing": "input_tokens_per_merge"}   # the `metrics.window` figure for each measure
CLEAR = 0.2   # a difference of at least this share of control's value is a real one

MEASURES = ("review_runs_per_landing", "minutes_per_landing", "input_tokens_per_landing")


@dataclass(frozen=True)
class Trial:
    name: str
    area: str
    started: date
    ended: date | None
    share: float
    measure: str
    min_landings: int
    max_days: int
    prompts: dict = field(default_factory=dict)   # template name -> variant file path
    policy: dict = field(default_factory=dict)    # policy name -> int


def _date(value, where: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{where} must be YYYY-MM-DD, not {value!r}") from None


def _whole(value, where: str, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise ValueError(f"{where} must be a whole number of at least {low}, not {value!r}")
    return value


def load(path: Path, prompt_names: Iterable[str] | None, policy_bounds: Mapping[str, tuple[int, int | None]] | None) -> list[Trial]:
    """The trials declared in `path`. Variant files are read under the folder holding `path`, and compared with the
    room's own template in its `prompts` folder. With `prompt_names` or `policy_bounds` None, that check is skipped
    (a reader that only reports results). Raises ValueError naming the trial and the field."""
    path = Path(path)
    ways = path.parent
    names = set(prompt_names) if prompt_names is not None else None
    with path.open("rb") as f:
        entries = tomllib.load(f).get("trial", [])
    trials: list[Trial] = []
    for i, e in enumerate(entries):
        name = e.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"trial #{i + 1}: name must be a non-empty string")
        if any(t.name == name for t in trials):
            raise ValueError(f"trial {name}: name is a duplicate")

        def w(fieldname: str) -> str:
            return f"trial {name}: {fieldname}"
        area = e.get("area")
        if not isinstance(area, str) or not area:
            raise ValueError(f"{w('area')} must be a non-empty string")
        started = _date(e.get("started"), w("started"))
        ended = _date(e["ended"], w("ended")) if "ended" in e else None
        share = e.get("share")
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share < 1:
            raise ValueError(f"{w('share')} must be strictly between 0 and 1, not {share!r}")
        measure = e.get("measure")
        if measure not in MEASURES:
            raise ValueError(f"{w('measure')} must be one of {', '.join(MEASURES)}, not {measure!r}")
        min_landings = _whole(e.get("min_landings"), w("min_landings"), 1)
        max_days = _whole(e.get("max_days"), w("max_days"), 1)
        variant = e.get("variant") or {}
        prompts, policy = dict(variant.get("prompts") or {}), dict(variant.get("policy") or {})
        if not prompts and not policy:
            raise ValueError(f"{w('variant')} changes nothing: give it prompts or policy")
        for pname, rel in prompts.items():
            if names is not None and pname not in names:
                raise ValueError(f"{w('variant.prompts')} names {pname!r}, which is not a prompt template")
            file = ways / str(rel)
            if not file.is_file():
                raise ValueError(f"{w('variant.prompts')} {pname}: file {rel!r} does not exist")
            own = ways / "prompts" / f"{pname}.md"
            if own.is_file():
                extra = set(PLACEHOLDER.findall(read_way(file))) - set(PLACEHOLDER.findall(read_way(own)))
                if extra:
                    raise ValueError(f"{w('variant.prompts')} {pname}: uses {{{{{sorted(extra)[0]}}}}}, "
                                     "which the room's template does not")
        for qname, value in policy.items():
            if policy_bounds is None:
                continue
            if qname not in policy_bounds:
                raise ValueError(f"{w('variant.policy')} names {qname!r}, which is not a policy number")
            low, high = policy_bounds[qname]
            if (isinstance(value, bool) or not isinstance(value, int) or value < low
                    or (high is not None and value > high)):
                raise ValueError(f"{w('variant.policy')} {qname}: {value!r} is outside its bounds "
                                 f"({low} to {high if high is not None else 'no ceiling'})")
        trial = Trial(name, area, started, ended, float(share), measure, min_landings, max_days, prompts, policy)
        for other in trials:
            if other.area == area and _overlap(other, trial):
                raise ValueError(f"trial {name}: area is {area!r}, already in use by trial {other.name} "
                                 "on the same days (one trial per area at a time)")
        trials.append(trial)
    return trials


def _overlap(a: Trial, b: Trial) -> bool:
    return ((a.ended is None or b.started <= a.ended) and (b.ended is None or a.started <= b.ended))


def active(trials: Iterable[Trial], today: date) -> list[Trial]:
    """The trials started on or before `today` and not ended before it."""
    return [t for t in trials if t.started <= today and (t.ended is None or t.ended >= today)]


def arm(trial: Trial, key: str) -> str:
    """"variant" or "control" for `key` (the dispatcher passes `<project>#<issue>`): the same answer every time, in
    every process, so a task keeps one arm for all its runs."""
    digest = hashlib.sha256(f"{trial.name}\0{key}".encode("utf-8")).digest()
    return "variant" if int.from_bytes(digest[:8], "big") / 2 ** 64 < trial.share else "control"


def _day_start(d: date) -> float:
    return datetime.combine(d, dtime.min).timestamp()


def _arms(trial: Trial, rows: list[dict]) -> dict[int, str]:
    """Row index -> arm, for the rows of tasks (and their PRs) whose runs recorded an arm in this trial."""
    prefix = trial.name + "="
    by_task: dict = {}
    for r in rows:
        for o in r.get("outputs") or []:
            ref = str(o.get("ref") or "")
            if o.get("kind") == "trial" and ref.startswith(prefix) and r.get("task"):
                by_task[r["task"]] = ref[len(prefix):]
    by_pr = {}
    for r in rows:
        pr = metrics.pr_of(r)
        if pr is not None and r.get("task") in by_task:
            by_pr[pr] = by_task[r["task"]]
    found = {}
    for i, r in enumerate(rows):
        a = by_task.get(r.get("task"))
        if a is None and (pr := metrics.pr_of(r)) is not None:
            a = by_pr.get(pr)
        if a in ("variant", "control"):
            found[i] = a
    return found


def result(trial: Trial, rows: list[dict], now: float) -> dict:
    """The trial's outcome from one project's ledger rows: {"arms": {"control"|"variant": {"landings", "value"}},
    "verdict": ...}. Landings are split by the arm recorded on the runs of the same task; a landing whose task's runs
    carry no arm for this trial counts in neither. `value` is the trial's measure per landing (metrics.window).
    Verdict: `running`, `variant better`, `variant worse`, `no clear difference` or `too few landings`."""
    since = _day_start(trial.started)
    until = min(now, _day_start(trial.ended + timedelta(days=1)) if trial.ended else now)
    arm_of = _arms(trial, rows)
    arms = {}
    for name in ("control", "variant"):
        part = [r for i, r in enumerate(rows) if arm_of.get(i) == name]
        figures = metrics.window(part, since, until, {})
        arms[name] = {"landings": figures["merges"], "value": figures[MEASURE_FIELD[trial.measure]]}
    both = all(a["landings"] >= trial.min_landings for a in arms.values())
    over = now >= _day_start(trial.started + timedelta(days=trial.max_days))
    if not both:
        verdict = "too few landings" if over else "running"
    else:
        c, v = arms["control"]["value"], arms["variant"]["value"]
        if c is None or v is None:
            verdict = "no clear difference"
        elif v < c and c - v >= CLEAR * c:
            verdict = "variant better"
        elif v > c and v - c >= CLEAR * c:
            verdict = "variant worse"
        else:
            verdict = "no clear difference"
    return {"arms": arms, "verdict": verdict}


def report(path: Path, rows: list[dict], now: float, only_active: bool = False) -> list[tuple[Trial, dict]]:
    """Each declared trial with its result; [] when the file is missing or malformed (a reader never stops for it)."""
    try:
        declared = load(path, None, None)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return []
    if only_active:
        declared = active(declared, datetime.fromtimestamp(now).date())
    return [(t, result(t, rows, now)) for t in declared]


def final(verdict: str) -> bool:
    return verdict != "running"


def line(trial: Trial, res: dict) -> str:
    """One plain line: the trial's name, each arm's landings and figure, and the verdict."""
    parts = []
    for name in ("control", "variant"):
        a = res["arms"][name]
        value = "n/a" if a["value"] is None else metrics.readable(a["value"])
        parts.append(f"{name} {a['landings']} landings, {trial.measure} {value}")
    return f"{trial.name} ({trial.area}): {'; '.join(parts)}: {res['verdict']}"
