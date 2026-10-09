# Common Room v2: design

A project-agnostic AI studio. It drives one project toward an explicit goal with Claude Code and Codex workers. It spends subscription allowance only on work that produces output, and it treats the owner as part of the team: the person who decides, and a person who gets assigned work.

Written in response to [postmortem-v1.md](postmortem-v1.md).

## Principles
1. **Goal first.** A project declares a mission and milestones, each with acceptance criteria. Every run belongs to a task, and every task belongs to a milestone. Work that serves no milestone doesn't happen.
2. **Output or stop.** Every run must leave an artifact: a commit, a PR, a merge, an issue or decision, a human task, or a report. A run of spending with no output trips a breaker and escalates with a diagnosis. It doesn't retry.
3. **Code schedules, models do the work.** Dispatch, pacing and gates are plain deterministic code. Models are called to build, review, and make judgments at defined triggers. There is never a polling planner.
4. **Workers can act.** A builder is a full Claude Code or Codex session in its own git worktree that runs the project's checks. Done means checks pass and a PR is open.
5. **One permission model.** Workers, the director and the owner's own sessions all load the same project config (`.claude/`, `CLAUDE.md`, `AGENTS.md`). The owner can open and continue any worker session. The room changes itself through ordinary PRs to this repo.
6. **The owner is both boss and staff.** The owner gets an approvals queue, plus assigned tasks with an exact deliverable, format, due date and the tasks they block. Owner suggestions are judged on their merits, and a wrong one is rejected plainly with the reason. An explicit `decided:` overrides the judgment, and any dissent is recorded.
7. **AI-native estimates.** Estimates are in runs, minutes and percent of allowance, and they're calibrated from the ledger. Feasibility is judged by what the agents can do, never by the owner's personal skills.
8. **Visible.** At any moment you can see what's running, which goal it serves, what it has cost and what it has produced.

## Parts
| Part | What it is | Status |
|---|---|---|
| Project config | `room.toml` in the project repo: mission, budgets, roles and models, checks, breakers | v2.0 |
| Ledger | SQLite at `~/.commonroom/room.sqlite3`: usage readings, runs, outputs, breaker trips | v2.0 |
| Usage readers | No inference. Claude: status-line tap, worker stream events, and the `/usage` screen as a fallback. Codex: app-server `account/rateLimits/read` | v2.0 |
| Gate | Reserves, weekly pacing, a cap on 5-hour burn rate, and breakers for no output and repeated failure | v2.0 |
| CLI | `room status`, `room usage`, `room gate`, `room tap`, `room probe` | v2.0 |
| Board | GitHub Issues and milestones. Labels: `kind:*`, `role:*`, `owner:human`, `size:*`, `status:*`. Cap on WIP in `status:ready` | v2.0 (by convention) |
| Dispatcher | `room run`: picks the top ready task whose role passes the gate, launches the worker, records the run, comments on the issue | built 2026-10-05 |
| Workers | `claude -p` (with the role prompt appended and no MCP) and `codex exec`, each in a dispatcher-made worktree. Session IDs are recorded so any run can be reopened | built 2026-10-05 |
| Director | An Opus session. It runs interactively with the owner, or headless on triggers: the milestone has no ready tasks, a PR is ready to merge, or something escalated. It also runs headless to steer (pace to the goal, the bottleneck, the allocation) once its last run is `[director] steer_every_hours` old (default 3, 0 turns it off; commonroom#41). Capped at 15% of spend, and each run must change the board or merge something | v2.1 |
| Dashboard | `room dashboard` at http://127.0.0.1:8770: usage against pace, live worker activity, runs with spend and output, the owner's queue, board, pause/resume, suggestion box | built 2026-10-05 |

## How the owner takes part
There are two channels, and they mean different things.

| | Executive channel | Room channel |
|---|---|---|
| Where | A Claude desktop session with the director, like the one this design came from | GitHub issues and PRs, plus `room` commands |
| Owner's role | Boss: sets direction, approves, overrides | Employee: delivers assigned work and makes suggestions |
| Weight | Binding. `decided:` statements are final, and the director records any dissent | Judged on merit. A wrong suggestion is rejected with the reason |
| Typical traffic | Milestone plans, scope, spend, architecture, "what's going wrong?" | Art deliveries, playtest reports, bug reports, ideas |

In the room channel:
- **Assigned work** arrives as issues labelled `owner:human`. Each states the exact deliverable, format, where to put it, a `Due:` date, and the tasks it blocks. `room status` lists them by due date.
- **Submitting work:** commit files to the path the issue names (for example `art/incoming/…`), or attach them to the issue, then comment `submitted`. The director reviews submissions like any other PR: accepted, or sent back with specific notes.
- **Suggestions:** open an issue labelled `kind:suggestion`. The director triages each one: accepted (it becomes a task with acceptance criteria), rejected (with reasons), or parked (with what would change the answer).
- **Playtests:** open an issue labelled `kind:playtest` with what you did and what felt wrong. The director turns those into tasks.

## The room serves itself
Common Room is a project of its own (commonroom#31). Its `room.toml` points at `kgaydhani/commonroom` and the milestone "Room v3: the room improves itself", and `AGENTS.md` and `.claude/agents/` tell its workers how to work on this repo. The heartbeat starts one Claude dispatcher for it next to the game's, with its own lock (`commonroom-claude.lock`), its own logs (`heartbeat-commonroom-claude.log`, `logs/commonroom/`) and its own worktrees under `D:/Dev/commonroom-wt/room/`. Its workers never touch the live checkout the room runs from.

- **Guest on the game's plan (`[share]`).** One ledger, one meter. The guest reads its budget gate (reserves, pace, burn cap, owner yield) from the host's `room.toml`, so the two can't drift apart. The host's room-wide stops (owner or auditor pause, no_progress) hold the guest too. The guest's model runs take at most `max` (15%) of all projects' autonomous run time per provider over the trailing `hours` (24). The exception is when no other project has used that provider for `idle_minutes` (60): then nobody is waiting on that allowance. The host is never capped by a guest. A guest's progress guarantee charges it only its share of the plan-wide spend.
- **Room workers are not the owner.** Every run records the folder its worker ran in. When a project checks whether the owner is using Claude elsewhere, it ignores those folders, so neither project mistakes the other's workers for the owner.
- **Merging without CI (`[checks] ci = false`).** Housekeeping merges on the newest `REVIEW: APPROVE` posted after the last commit, but only when that approval carries `TESTS: PASS <head sha> <test command>: <result>`. The sha has to match the PR head, because a commit pushed during a review can still predate the review comment. An approval without that line goes back for review, and the review run counts as failed, so the no-output breaker stops a loop. `review:skip` doesn't apply without CI. As for every project, main moves one merge per housekeeping pass, and only a branch that is up to date with main merges (commonroom#19). When the room merges main into a branch, the branch gets a new head, so the merged tree is reviewed and tested again before it lands.
- **Director paths (`[merge] director_paths`).** A PR that changes a file matching one of these globs (commonroom: `gate.py`, `config.py`, `room.toml`) merges only when, on top of the usual approval, a comment starting with `[agent:director]` and holding `REVIEW: APPROVE` follows its last commit. The director's verdict doesn't count as the reviewer's. Housekeeping asks for it only once the branch is up to date with main, so the director judges the tree main becomes and the update doesn't void their approval. While such a PR waits only on the director, `director_trigger` names it. A director `REVIEW: CHANGES` after the last commit sends the PR back to its builder, as a reviewer's does, and the trigger stops naming it. Unset, nothing changes (commonroom#49).
- **Updating the live checkout.** After merging into the room's own repo, housekeeping runs `git pull --ff-only` in the checkout the room runs from. It never resets or forces: a diverged, dirty-conflicting or non-`main` checkout is left alone and reported. This is safe with room processes running:
  - Running python processes keep the code they loaded. Room modules are imported at start, never inside functions, and a test enforces that.
  - Workers never run from the live checkout.
  - `heartbeat.cmd` is one block that cmd.exe reads whole before running any of it.

  Each process picks up new code when it next starts. The until-idle dispatchers restart on the next heartbeat after they go idle.

## Autonomy and the progress guarantee
When the owner is away, a scheduled heartbeat runs `room run`. It dispatches ready tasks while the gate allows, and it triggers the director only for the events listed under Parts. One part of autonomous time is reserved for improving the room itself: a daily review of the ledger (spend versus output, failure patterns, slow steps) that can produce at most one improvement PR to this repo.

**The guarantee:** if autonomous Claude spend over a rolling 24 hours exceeds 10% of the weekly window and nothing was merged in that period, the room stops autonomous work and writes a diagnosis to the owner's queue. "Lots of usage, nothing built" can last a day at most, never a weekend.

## Budget policy (defaults; override in `room.toml`)
- **Reserves:** Claude keeps 25% of the weekly window and 10% of each 5-hour window for the owner. Codex has no reserve and may be run dry.
- **Weekly pacing:** autonomous Claude spend may not run ahead of `(100 − reserve) × elapsed_fraction + slack`. When it does, autonomous Claude work holds until the pace catches up. The owner's interactive sessions are never blocked.
- **5-hour burn cap:** within a window, autonomous Claude spend is held to `(100 − reserve) × min(1, elapsed + front_load)`. A fresh window can't be emptied in minutes.
- **Unknown readings:** an unknown or stale reading is not free capacity. Autonomous work waits for a fresh one.
- **No-output breaker:** if the last 3 finished autonomous runs produced nothing, autonomy stops until someone runs `room breaker reset`, with a diagnosis attached.
- **Repeated-failure breaker:** a task that fails twice goes to the director, never to an 18th attempt.

## Roles (defaults)
| Role | Default | Notes |
|---|---|---|
| director | Claude Opus | Plans milestones, writes briefs, merges, deals with the owner |
| builder | Codex first, then Claude Sonnet | Codex can be run dry, so it takes routine builds |
| builder-heavy | Claude Opus | Hard or architectural tasks |
| reviewer | the provider that didn't build it | Codex reviews Claude's work and the other way round |
| scout | Claude Haiku | Researches tools and approaches, proposes experiments, never edits code |
| analyst | Claude Haiku | Summarises test and simulation output |

## Roadmap
- **v2.0:** ledger, usage readers, gate, CLI.
- **v2.1:** dispatcher, worker runner, issue comments, Codex reviewer.
- **v2.2:** dashboard, approvals, the owner's queue, digests and notifications.
- **v2.3:** estimate calibration from the ledger, breaker tuning, more projects.
