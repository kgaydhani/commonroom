# Verification for #457

- Faked Git and GitHub: a changed Codex build reporting `TESTS: INCOMPLETE` and `STATUS: BLOCKED` is published and sent to review when pre-review testing is active, without `provider:claude`.
- With the flag off, no changes, a failed suite, or another `Blocked:` / `BLOCKER` line, the existing handoff remains. Claude still escalates, and assessment requires a new published head.
- The new guidance appears only in Codex build prompts with active pre-review testing. Existing golden prompts remain unchanged; one synthetic pretesting fixture was added.
- Read-only configuration checks confirmed gamemirage and magazine do not enable `test_before_review`. Neither project's configuration was changed.
- Prompt budgets pass: the new rendered fixture is 554/700 tokens; all templates total 3185/3200.
- Focused command: `python -m unittest tests.test_codex_incomplete tests.test_prompts tests.test_prompt_golden tests.test_prompt_budget tests.test_ways`.

```text
Ran 36 tests in 29.636s
OK
```

- Full command: `python "D:/Dev/commonroom/room.py" check`, from the assigned worktree, exit 3. The sandbox could not acquire the suite lock, so the runner used its unlocked fallback and then reached its time limit:

```text
suite ran out of time after 9 min: report TESTS: INCOMPLETE
```

TESTS: INCOMPLETE. No full-suite summary was produced; a complete run outside the sandbox remains unverified. The room tests the published PR head before review.

All changed files use LF and `git diff --check` passes. Running dispatchers retain their imported code; the change activates on their next normal start. No live process was stopped or restarted. Git operations and PR creation are delegated to Common Room by the task instructions.
