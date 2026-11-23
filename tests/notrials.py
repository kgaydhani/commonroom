"""Point the room at an empty trials.toml for a whole test module (commonroom#449).

A dispatcher pass reads the shipped `commonroom/ways/trials.toml` (`dispatch.TRIALS_FILE`), and the digest reads
`trials.FILE`. Once a declared trial passes its `max_days`, the sweep reports a `trial-final` finding, so a test that
runs a pass on today's date would start failing on a calendar day. Modules that run passes use this instead:

    setUpModule, tearDownModule = notrials.start, notrials.stop
"""
import tempfile
from pathlib import Path
from unittest import mock

from commonroom import dispatch, trials

_active = []


def start():
    tmp = tempfile.TemporaryDirectory()
    empty = Path(tmp.name) / "trials.toml"
    empty.write_text("", encoding="utf-8")
    patches = [mock.patch.object(dispatch, "TRIALS_FILE", empty), mock.patch.object(trials, "FILE", empty)]
    for p in patches:
        p.start()
    _active.append((tmp, patches))


def stop():
    tmp, patches = _active.pop()
    for p in reversed(patches):
        p.stop()
    tmp.cleanup()
