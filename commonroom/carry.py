"""Carry a tested approval across clean merges of main (commonroom#226). No CI: a PR merges on its reviewer's approval
with `TESTS: PASS <head sha>`, so every merge of main into it used to cost a model re-review of a tree that only main
changed. Code runs the project's tests on the new head instead, and a room comment records the result:

    TESTS: PASS <head sha> <command>: <result line>
    CARRIES: APPROVE <approval createdAt> TESTED <sha the reviewer tested>

The board adds the `[room]` mark when it posts; `parse` takes it once or more. Plain functions here; the Dispatcher decides when a PR qualifies."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from commonroom import workers as workers_mod
from commonroom.lock import claim_lock, release_lock

RESULT = re.compile(r"\A\s*(?:\[room\]\s*)+TESTS: (PASS|FAIL|INCOMPLETE) ([0-9a-f]{7,40}) (.*)$", re.M)
CARRIES = re.compile(r"^CARRIES: APPROVE (\S+) TESTED ([0-9a-f]{7,40})\s*$", re.M)
HEX = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)
DEFAULT_MAX_MINUTES = 10   # a hung suite gives the housekeeping lock back after this; `[checks] max_minutes` overrides


def comment(result: str, head: str, command: str, line: str, approval_at: str, tested: str, url: str = "") -> str:
    """The body of the room's comment on a PR (the board adds the `[room]` mark) after it ran the tests on `head` for the approval posted at `approval_at`."""
    first = f"TESTS: {result} {head} {command}: {line}"
    if result == "PASS":
        return (f"{first}\nCARRIES: APPROVE {approval_at} TESTED {tested}\n\nThis head differs from {tested[:7]}, which "
                f"the reviewer approved and tested ({url or approval_at}), only by clean merges of main, and the tests pass "
                f"on it, so that approval stands for it with no new review (commonroom#226).")
    why = "failed" if result == "FAIL" else "could not finish"
    return (f"{first}\n\nThe tests {why} on this head, which differs from the approved {tested[:7]} only by clean merges "
            f"of main, so the approval is not carried: the PR goes back to review (commonroom#226).")


def parse(body: str | None) -> dict | None:
    """{result, head, line, approval_at, tested} from a room test comment, else None. `approval_at` and `tested` are
    '' unless it is a PASS that names the approval it carries."""
    m = RESULT.match(body or "")
    if not m:
        return None
    c = CARRIES.search(body or "") if m.group(1) == "PASS" else None
    return {"result": m.group(1), "head": m.group(2).lower(), "line": m.group(3),
            "approval_at": c.group(1) if c else "", "tested": c.group(2).lower() if c else ""}


FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[`~]*[ \t]*$", re.M | re.S)
CODE_SPAN = re.compile(r"(`+)(?!`)[^\n]*?(?<!`)\1(?!`)")


def without_code(body: str | None) -> str:
    """The comment with each closed fenced code block and inline code span that holds a `TESTS:` marker removed. A marker
    inside code is a quote of a verdict, not a verdict (commonroom#283). Code without a marker stays, so a command in
    backticks on a PASS line still counts. An unclosed fence is left in: when in doubt a marker still blocks."""
    def drop(m):
        return "" if re.search(r"TESTS:", m.group(0), re.I) else m.group(0)
    return CODE_SPAN.sub(drop, FENCE.sub(drop, body or ""))


def tested_sha(body: str | None, oids: list[str]) -> str:
    """The PR commit the approval's `TESTS: PASS` line names (it may give 7 characters), or ''."""
    for line in without_code(body).splitlines():
        if re.search(r"\bTESTS:\s*PASS\b", line, re.I):
            for token in HEX.findall(line):
                found = [o for o in oids if o.lower().startswith(token.lower())]
                if len(found) == 1:
                    return found[0].lower()
    return ""


def summary(output: str) -> str:
    """The last two non-empty lines of the run's output, as one line: `Ran 524 tests in 80.163s OK` for unittest."""
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    return " ".join(lines[-2:])[:200] or "(no output)"


def run_suite(root: Path, head: str, command: str, worktree: Path, lock: Path, max_minutes: float,
              env_for=workers_mod.worker_env) -> tuple[str, str]:
    """Run `command` on commit `head` in a temporary detached worktree, under the per-project suite lock `room.py
    check` uses. Returns (PASS|FAIL|INCOMPLETE|BUSY, result line). BUSY: another run holds the lock; nothing ran."""
    try:
        if claim_lock(lock) is not None:
            return "BUSY", "the suite lock is held by another run"
    except OSError as exc:
        return "BUSY", f"the suite lock is unavailable: {exc}"
    tree, log = workers_mod.ProcessTree(), worktree.parent / f"{worktree.name}.log"
    try:
        worktree.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, capture_output=True)
        added = subprocess.run(["git", "worktree", "add", "--detach", str(worktree), head], cwd=root,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        if added.returncode:
            return "INCOMPLETE", f"could not check out {head[:7]}: {added.stderr.strip()[:150]}"
        with log.open("w", encoding="utf-8") as out:
            process = tree.start(command, shell=True, cwd=worktree, stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env_for("claude", worktree))
            try:
                code = process.wait(timeout=max_minutes * 60)
            except subprocess.TimeoutExpired:
                return "INCOMPLETE", f"still running after {max_minutes:g} min; stopped"
        line = summary(log.read_text(encoding="utf-8", errors="replace"))
        return ("PASS" if code == 0 else "FAIL"), (line if code == 0 else f"exit {code}: {line}")
    except OSError as exc:
        return "INCOMPLETE", f"{type(exc).__name__}: {str(exc)[:150]}"
    finally:
        tree.end()   # the test command and anything it started, so nothing outlives the bound
        try:
            if tree.process is not None:
                tree.process.wait(timeout=15)
            log.unlink(missing_ok=True)
        except (OSError, subprocess.TimeoutExpired):
            pass
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, capture_output=True)
        shutil.rmtree(worktree, ignore_errors=True)
        shutil.rmtree(workers_mod.worker_tmp(worktree), ignore_errors=True)
        release_lock(lock)
