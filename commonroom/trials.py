"""Ways-of-working trials: the declaration and the arm assignment (commonroom#350). Pure code: no GitHub calls, no
clock reads (`today` is a parameter), and it imports neither `dispatch` nor `policy`; callers pass the prompt names
and policy bounds."""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from commonroom.waytext import PLACEHOLDER, read_way

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


def load(path: Path, prompt_names: Iterable[str], policy_bounds: Mapping[str, tuple[int, int | None]]) -> list[Trial]:
    """The trials declared in `path`. Variant files are read under the folder holding `path`, and compared with the
    room's own template in its `prompts` folder. Raises ValueError naming the trial and the field."""
    path = Path(path)
    ways = path.parent
    names = set(prompt_names)
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
            if pname not in names:
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
