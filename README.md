# Common Room v2

A goal-driven AI studio for one owner. It runs Claude Code and Codex workers against a project's GitHub board, spends subscription allowance only on work that produces output, and treats the owner as both boss (executive channel) and team member (room channel).

- Why it exists, and what went wrong with v1: [docs/postmortem-v1.md](docs/postmortem-v1.md)
- How it works: [docs/design.md](docs/design.md)

## Use
Run from anywhere inside a project that has a `room.toml`:

```
python D:/Dev/commonroom/room.py status          # goal, your queue, usage, runs, breakers
python D:/Dev/commonroom/room.py usage           # meter readings and gate verdicts
python D:/Dev/commonroom/room.py gate claude     # exit 0 if autonomous Claude work may run now
python D:/Dev/commonroom/room.py probe codex     # read the Codex meter (no inference)
python D:/Dev/commonroom/room.py breaker reset   # clear a tripped breaker after reading the diagnosis
```

`room tap` is a Claude Code status-line command. It records your Claude rate limits on every response and prints a compact usage line.

The ledger lives at `~/.commonroom/room.sqlite3`.

## Moonshots

Moonshots run one long Claude session on a dedicated branch, with an external scorecard loop and a budget in **active minutes**. Usage pauses and shutdown downtime do not spend that budget.

```
room moonshot add experiment --brief brief.md --minutes 180 --model fable --effort high
room moonshot status experiment
room moonshot note experiment feedback.md
room moonshot stop experiment
room moonshot resume experiment --extend 60 --prompt-file followup.md
room moonshot run                    # launch due jobs / recover dead runners
room moonshot run experiment         # run one job in the foreground
```

The heartbeat's housekeeping launches up to three jobs, each in a detached process. `add` queues a job; `--start HH:MM|epoch` delays launch, `--base` selects its starting ref (default `origin/main`), and `--until HH:MM|epoch` sets an optional hard deadline. A passed local clock time means tomorrow. `list` and `status` show usage, rounds, scorecard totals, session and cost.

Briefs and project rules from `.claude/moonshot_rules.md` are copied to `~/.commonroom/moonshots/<project>/<name>/brief.md` (built-in rules apply when absent). Work stays in `.claude/worktrees/ms-<name>` on `moonshot/<name>-<date>`. The default report is `docs/moonshot/<date>-<name>.md`; its sibling `.scorecard.json` follows this contract:

```json
{"round": 2, "rows": [{"id": "formation", "bar": "shields in front at contact in >= 90% of seeds", "value": "94%", "met": true}]}
```

Use `--report`, `--verify-cmd` and `--max-rounds` to customize the Gauntlet. The verifier runs in the worktree and emits the same JSON format; its rows override session rows by id. Missing or invalid scorecards trigger another round when more than 25 minutes remain. Unmet and regressed rows are named in the next prompt. At 20 minutes the harness interrupts to request deliverables. Feedback is prepended verbatim to the next prompt, including after a pause or restart. Finished jobs save and push their branch and create a `needs:director` harvest issue; they never open PRs or merge. Worktrees remain available for harvest or resumption. Logs live at `~/.commonroom/logs/moonshots/<project>-<name>.log`.

Defaults can be overridden in `room.toml`:

```toml
[moonshot]
minutes = 180
model = "fable"
effort = "high"
base = "origin/main"
max_rounds = 5
max_parallel = 3
pause_5h = 80
stop_model_weekly = 85
stop_weekly = 85
```

Launches use the room's normal gate. Running jobs pause at the 5-hour threshold until reset, and stop above either weekly threshold. The ledger checkpoints active time every five seconds; a sudden shutdown can lose roughly the last five seconds of accounting. `COMMONROOM_HOME` relocates durable state and logs.

## Develop
```
python -m unittest discover -s tests -v
```
The core uses only the standard library. The optional `/usage` screen reader needs `pywinpty` and `pyte` (`uv pip install pywinpty pyte` into `.venv`).
