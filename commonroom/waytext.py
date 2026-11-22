"""Reading and filling the room's prompt templates. Apart from `dispatch` so that `handshake` can use them without an
import cycle (commonroom#345); `dispatch` re-exports the names."""
from __future__ import annotations

import re
from pathlib import Path

PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def read_way(path: Path) -> str:
    """One template's text, without the final newline an editor adds (no prompt ends in one)."""
    text = path.read_text(encoding="utf-8-sig")
    return text[:-1] if text.endswith("\n") else text


def fill_way(template: str, **values) -> str:
    """`template` with each `{{name}}` replaced by its value, in one pass: a value is never expanded again."""
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)
