# Coordination share verification (#323)

All test data used temporary ledgers and made-up projects. `COMMONROOM_HOME` pointed
inside the worktree's scratch directory; no live ledger was used.

The requested full-suite command, run from the worktree root:

```text
python "D:/Dev/commonroom/room.py" check
suite ran out of time after 9 min: report TESTS: INCOMPLETE
```

The full suite remains unverified. A separate attempt reported
`suite lock busy: report TESTS: INCOMPLETE`.

A focused run used the same `check` lock with an in-memory override of its test
command, preserving the existing sandbox compatibility path in `PYTHONPATH`:

```text
python -m unittest test_metrics.CoordinationShare test_metrics.Metrics test_metrics.Targets test_axioms test_ways
Ran 41 tests in 6.461s
OK
```

This covers all six new coordination tests, unchanged per-merge metrics and target
tests, the decisions ledger validator, and the AX-A1 prompt guard. It checks mixed
roles, missing/zero tokens, zero minutes, window exclusions, cap comparisons
(including equality and an unrounded excess), CLI JSON output, dashboard data,
and separation between two made-up projects. The unrelated `CacheSplit` worker
tests were outside this focused selection.

`git diff --check` passed, and every changed file uses LF. No `room.toml`, budget,
gate, or default target changed. Existing dashboard processes load the new metrics
on their next normal start; no process was restarted for this change.
