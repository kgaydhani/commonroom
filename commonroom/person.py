"""The owner's person file (commonroom#573, docs/layer.md conclusion 2): one small file in the room home, `person.md`,
with a hard cap, one dated line per fact. Long instruction files and injected memory cost tokens and lower task
success, so the cap is enforced here, in code, and never raised by a project: the limits are `person_max_lines` and
`person_max_tokens` in commonroom/ways/policy.toml. Tokens are estimated as characters / 4, rounded up.

Line format: `YYYY-MM-DD text`. `add` refuses a line that looks like a secret (`SECRET_PATTERNS`) and any addition
that would pass either limit, listing the oldest lines so the owner can remove one."""
from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from pathlib import Path

from commonroom import ledger as ledger_mod, policy

FILENAME = "person.md"
MAX_LINES = policy.DEFAULTS["person_max_lines"]
MAX_TOKENS = policy.DEFAULTS["person_max_tokens"]
OLDEST_SHOWN = 5
# What looks like a token or password. Short on purpose: a refusal costs the owner one reworded line.
SECRET_PATTERNS = (
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("an API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("a bearer token", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
    ("a password or secret", re.compile(r"\b(?:pass(?:word|wd)?|pwd|secret|token|api[_ -]?key)\b\s*(?:is\s*)?[:=]\s*\S{4,}", re.I)),
    ("a long random string", re.compile(r"\b[A-Za-z0-9+/_-]{40,}\b")),
)
DATED = re.compile(r"^\d{4}-\d{2}-\d{2} \S")


class PersonError(Exception):
    """A change refused; the message says why."""


def path(home: Path | None = None) -> Path:
    return (home if home is not None else ledger_mod.default_path().parent) / FILENAME


def tokens(lines: list[str]) -> int:
    """Estimated tokens: characters / 4 rounded up, counting each line's newline."""
    return -(-sum(len(x) + 1 for x in lines) // 4)


def load(home: Path | None = None) -> list[str]:
    target = path(home)
    if not target.exists():
        return []
    return [x for x in target.read_text(encoding="utf-8").splitlines() if x.strip()]


def save(lines: list[str], home: Path | None = None) -> None:
    target = path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=FILENAME, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(x + "\n" for x in lines))
    os.replace(tmp, target)


def secret_kind(text: str) -> str | None:
    return next((kind for kind, rx in SECRET_PATTERNS if rx.search(text)), None)


def add(text: str, home: Path | None = None, today: date | None = None) -> list[str]:
    """Add one dated line and return the file's lines. Raises PersonError for an empty or multi-line text, a secret,
    or an addition past either cap (naming the oldest lines)."""
    text = " ".join(text.split())
    if not text:
        raise PersonError("nothing to add: the line is empty")
    kind = secret_kind(text)
    if kind:
        raise PersonError(f"refused: the line looks like {kind}. The person file holds no secrets; reword it.")
    lines = load(home)
    new = f"{(today or date.today()).isoformat()} {text}"
    after = lines + [new]
    if len(after) > MAX_LINES or tokens(after) > MAX_TOKENS:
        oldest = "\n".join(f"  {i}. {x}" for i, x in enumerate(lines[:OLDEST_SHOWN], 1))
        raise PersonError(f"refused: the file would pass its cap ({len(after)} lines of {MAX_LINES}, about "
                          f"{tokens(after)} tokens of {MAX_TOKENS}). Remove one with `room person remove <n>`. "
                          f"Oldest lines:\n{oldest}")
    save(after, home)
    return after


def remove(n: int, home: Path | None = None) -> str:
    """Remove line `n` (1-based) and return it. Raises PersonError when there is no such line."""
    lines = load(home)
    if not 1 <= n <= len(lines):
        raise PersonError(f"no line {n}: the file has {len(lines)}")
    gone = lines.pop(n - 1)
    save(lines, home)
    return gone
