[agent:reviewer] REVIEW: APPROVE

TESTS: PASS 762cb7a python -m unittest discover -s tests: Ran 706 tests in 140.196s OK

**How I ran it.** I ran `python D:/Dev/commonroom/room.py check` from this worktree on 762cb7a, which is the PR head, with a clean tree. It exited 0. `TMP`/`TEMP`/`TMPDIR` pointed at a neutral folder so the `test_hook.HandshakeOnly` path match (#277) doesn't fire. That test isn't touched by this PR.

**Acceptance criteria**
- **Kill at `max_minutes`: met.** `_run_limited` waits on a single `time.monotonic()` deadline. At the deadline it kills only its own child's tree: `taskkill /T /F /PID <child>` on Windows, or `killpg` on a new session on POSIX. `test_suite_past_max_minutes_is_killed` uses a temp `room.toml` with `max_minutes = 0.1` and checks exit 3, the `suite ran out of time after 0.1 min: report TESTS: INCOMPLETE` text, that the grandchild PID is gone and that the lock file is released. It passed in this run on Windows.
- **Exit code passes through: met.** `test_suite_inside_the_limit_passes_its_code` (5) and the default-limit test (7, `wait(timeout=540)`) both pass, which also pins the 9-minute default.
- **Lock wait capped by `max_minutes`: met.** `deadline = min(started + wait, limit)`. A lock that frees after the deadline is not claimed. A suite whose allowance has already run out is never started. `test_lock_wait_is_capped_by_max_minutes` (live holder, `lock_wait_minutes = 10`) and the two-ordering subtest cover this.
- **Lock released on every exit path: met.** The locked run sits in `try/finally: release_lock`, and `test_start_failure_releases_the_lock` covers a `Popen` that raises. The busy and unlocked paths never hold the lock.
- **Prompt note: met.** `ways/prompts/suite.md` feeds both the builder and reviewer prompts and now says exit 3 means `TESTS: INCOMPLETE` / `REVIEW: INCOMPLETE`, never FAIL. The golden files are updated and `CheckPrompts` asserts the phrase.
- **No live ledger or `~/.commonroom`: met.** The tests use a temp `COMMONROOM_HOME`, and `local_check` patches the config load and cwd to the temp project.

**AGENTS.md.** `signal` is imported at the top of the module, and nothing is imported inside a function. No project name, path or login is hard-coded. Thresholds, reserves, caps and breakers are unchanged. The only behaviour change for projects is the one the issue asks for: a 9-minute default cut-off, with no `room.toml` lines needed. This repo's suite takes about 140 s, well inside it. The change is about 60 lines of code plus tests and golden files, all in scope.

**Findings (non-blocking)**
1. `[checks] max_minutes` is now read by two places with different defaults. `carry.py` (the housekeeping run) defaults to 10, and `check` defaults to 9. A project that sets `max_minutes` above 10 for carry would also raise `check` past the 10-minute worker tool limit, which is the problem this issue fixes. A follow-up could cap `check` at 9 or give it its own key, and mention `check`'s limit in `docs/design.md`.
2. `docs/i203-validation.txt` is the Codex sandbox log. It reports a failing suite (sandbox-only causes: #279 and #184) and says "Changes are left uncommitted". Both are now stale. Delete it in a follow-up.
3. The head commit is titled "WIP: unfinished work preserved by Common Room". Squash with the PR title as the message.
