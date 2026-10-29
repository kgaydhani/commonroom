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
| Background roles | `[roles] background = ["extractor"]` lists on-demand roles whose `role:<name>` issues never queue with builds. They run only after builds, reviews, research and the capability sweep find nothing, and only in spare allowance: the gate allows the provider, `weekly_pace - weekly_used >= [parallel] min_pace_headroom`, the owner is away, the time is inside the optional `background_window = "HH:MM-HH:MM"` (local, may wrap midnight), and no other autonomous run on that provider is running in any project, so the run's `weekly_delta` measures it alone. Parallel slots never take them. The project feeds the role with its own `[schedule.<role>]` and the role's agent drains the project's queue inside its run; the room never reads the project's database | built 2026-10-24 |
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
- **Work owned outside the room:** an issue the interactive director gives to its local agents or a cloud session gets `claim:external`. The room never builds it, never releases it as a stale claim and never puts `status:ready` on it. A conflict or red CI on its PR is noted on the issue once, and an approved PR still merges as usual. To hand it back to the room, remove the label (commonroom#132).
- **Submitting work:** commit files to the path the issue names (for example `art/incoming/…`), or attach them to the issue, then comment `submitted`. The director reviews submissions like any other PR: accepted, or sent back with specific notes.
- **Suggestions:** open an issue labelled `kind:suggestion`. The director triages each one: accepted (it becomes a task with acceptance criteria), rejected (with reasons), or parked (with what would change the answer).
- **Playtests:** open an issue labelled `kind:playtest` with what you did and what felt wrong. The director turns those into tasks.

## The room serves itself
Common Room is a project of its own (commonroom#31). Its `room.toml` points at `kgaydhani/commonroom` and the milestone "Room v3: the room improves itself", and `AGENTS.md` and `.claude/agents/` tell its workers how to work on this repo. The heartbeat starts one Claude dispatcher for it next to the host project's, with its own lock (`commonroom-claude.lock`), its own logs (`heartbeat-commonroom-claude.log`, `logs/commonroom/`) and its own worktrees under `D:/Dev/commonroom-wt/room/`. Its workers never touch the live checkout the room runs from.

- **Guest on the host's plan (`[share]`).** One ledger, one meter. The host is the project the heartbeat runs for: `heartbeat.cmd` sets `COMMONROOM_HOST` to that folder, and it overrides `[share] host` in any project that has a `[share]` table, so switching projects is a scheduled-task change. `[share] host` is the fallback for commands run by hand, and a project never hosts itself (commonroom#151). The guest reads its budget gate (reserves, pace, burn cap, owner yield) from the host's `room.toml`, so the two can't drift apart. The host's room-wide stops (owner or auditor pause, no_progress) hold the guest too. The guest's model runs take at most `max` (15%) of all projects' autonomous run time per provider over the trailing `hours` (24). The exception is when no other project has used that provider for `idle_minutes` (60): then nobody is waiting on that allowance. The host is never capped by a guest. A guest's progress guarantee charges it only its share of the plan-wide spend.
- **Room workers are not the owner.** Every run records the folder its worker ran in. When a project checks whether the owner is using Claude elsewhere, it ignores those folders, so neither project mistakes the other's workers for the owner.
- **Merging without CI (`[checks] ci = false`).** Housekeeping merges on the newest `REVIEW: APPROVE` posted after the last commit, but only when that approval carries `TESTS: PASS <head sha> <test command>: <result>`. The sha has to match the PR head, because a commit pushed during a review can still predate the review comment. An approval without that line goes back for review, and the review run counts as failed, so the no-output breaker stops a loop. `review:skip` doesn't apply without CI. As for every project, main moves one merge per housekeeping pass, and only a branch that is up to date with main merges (commonroom#19). When the room merges main into a branch, the branch gets a new head, so the merged tree is reviewed and tested again before it lands.
- **Project prompt guidance (`[prompts]`).** Room prompts name only the project's `[checks] test` command. Anything tool-specific comes from the project's room.toml: `[prompts] headless` is appended to the headless note every worker gets, and `[prompts] codex` is the sandbox and tooling note in Codex build and review prompts. An unset key adds nothing (commonroom#45).
- **Codex scratch folders (`[codex] scratch_env`).** The Codex sandbox writes only inside the worktree. Each environment variable the project lists (gamemirage: `APPDATA`, where Godot writes) points a Codex worker at `<worktree>/.scratch/<name lowercased>`, made before launch and kept out of `git status` by the repo's git exclude. Whatever the project sets, a Codex worker's `TEMP`, `TMP` and `TMPDIR` also point at `<worktree>/.scratch/tmp`, because the sandbox can't write to the shared `commonroom-tmp` folder Claude workers use and `tempfile` there failed with WinError 5 (commonroom#158). Claude workers keep `commonroom-tmp`, and get no other change (commonroom#81).
- **Bulletin watchdog (`[bulletin]`).** The owner's digest is due on issue `issue` by each local `deadlines` time. Once a deadline `D` passes with no comment starting with `@<repo owner>` since the deadline before it (bulletins go out early) and no director run since `D`, `director_trigger` adds `bulletin missed (due HH:MM)`: the director posts the digest and logs that the interactive session's schedules need recreating. It fires at most once per deadline. Unset, nothing changes (commonroom#82).
- **Owner notifications (`[notify]`).** The room acts on GitHub as the owner's own account, so its `@mentions` and assignments never notify the owner. With `ntfy_topic` set (and optionally `ntfy_server`, default `https://ntfy.sh`), housekeeping pushes to the owner's phone: each new open `owner:human` issue (several in one pass go as one notification), each new comment that mentions `@<owner>` (bulletins included; at most 5 a pass, the rest summed up), and a breaker stop at once at high priority. The first pass only sets the comment watermark, so history isn't replayed. A failed push is logged and retried next pass. The owner is `[project] owner`, default the repo's owner. Unset, nothing is sent (commonroom#151).
- **Only trusted logins count (`[project] trusted`).** The repos are public, so anyone can open an issue or PR, or comment. Every board read the dispatcher makes goes through `TrustedBoard`: it keeps only issues and comments whose `author.login` is the owner (agents post as the owner's account, so their comments still count) or a login in `[project] trusted` (a list, e.g. a co-editor; default empty), compared case-insensitively, and drops anything with no author. So a stranger's `REVIEW: APPROVE`, director verdict, comment on an `owner:human` task, `@owner` mention, bulletin or `status:ready` issue does nothing. A PR counts only when its head is a branch of this repository (`isCrossRepository` false, head owner the repo's owner) and its author is trusted: any other open PR is never reviewed, updated, labelled or merged, and housekeeping reports it once, in its log and to the owner's phone. The dashboard's owner queue shows only trusted comments too (commonroom#176).
- **Scheduled role runs (`[schedule.<role>]`).** Housekeeping opens a `role:<role>` + `status:ready` issue in the goal milestone at each `at` local time (`"07:00"` or a list) or every `every_hours`, with `title` and `body` (`{date}` and `{weekday}` are filled in) and any extra `labels`. A first run is due at once. While the last one it opened is still open, none opens. The role needs a `[roles]` entry and an agent file (or is `builder` or `scout`). Unset, nothing is scheduled (commonroom#151).
- **Heartbeat commands (`[heartbeat] commands`).** `room tick`, run by every heartbeat, runs these argv lists in the project folder, in order (`"{python}"` is the room's interpreter), so a per-project job such as a queue runner rides the heartbeat of its own project only. Unset, nothing runs (commonroom#151).
- **Director log (`[director] log_issue`).** The steering trigger names this issue (a number, or `owner/repo#N`) as where the Director log goes. Unset, it names none.
- **Director paths (`[merge] director_paths`).** A PR that changes a file matching one of these globs (commonroom: `gate.py`, `config.py`, `room.toml`) merges only when, on top of the usual approval, a comment starting with `[agent:director]` and holding `REVIEW: APPROVE` follows its last commit and has a line `HEAD: <sha>` that names the PR head (at least 7 characters, a prefix of `headRefOid`), so a commit made before the approval but pushed after it doesn't ride on it. A missing or stale `HEAD:` line means not approved yet, and the trigger text gives the director the sha to post (commonroom#55). The director's verdict doesn't count as the reviewer's. Housekeeping asks for it only once the branch is up to date with main, so the director judges the tree main becomes and the update doesn't void their approval. While such a PR waits only on the director, `director_trigger` names it. A director `REVIEW: CHANGES` after the last commit sends the PR back to its builder, as a reviewer's does, and the trigger stops naming it. Unset, nothing changes (commonroom#49). A PR labelled `needs:director` waits the same way whatever files it touches: the builder adds the label when the change needs lines in another project's `room.toml` or prompts, and the director lands them there before approving (commonroom#65).
- **Where housekeeping runs.** Besides the start of each slot-1 Claude pass, the heartbeat starts `room.py housekeep` for the host project and for Common Room: a pass now and every 3 minutes for 14 minutes, with no job and no gate, so merges and unblocking don't wait behind a long build. A per-project `<name>-housekeep.lock` keeps two passes from running at once, so one merge per pass still holds (commonroom#160).
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

Issues sharing an `area:<name>` label (or `risk:golden`, treated as one more area) are never built at once: none is offered while another with a shared area is in progress, in review, running or sent back to merge main, and with none held only the top-priority one per area is offered (commonroom#161). The director labels issues by the files they touch.

`[roles] builder_by_label = { "kind:research" = ["codex", "claude:opus"] }` sends builds carrying that label to that provider list first, unless the issue is `size:L`, `risk:high`, `builder:heavy` or pinned with `provider:*`; without the key nothing changes (commonroom#118).

Director, builder, reviewer, scout and auditor have their own triggers. Any other role with a `[roles]` entry and an agent
file `.claude/agents/<name>.md` (playtester, analyst...) runs on demand: an issue in the goal milestone labelled
`role:<name>` and `status:ready` queues with the builds by priority, runs that agent in a detached worktree, and is closed
with the agent's report when it ends with `STATUS: DONE` (commonroom#4). With a `[playtester]` table (`paths` globs,
`every_hours`, default 6), housekeeping opens `Playtest after merges: #a` (`role:playtester`, `status:ready`, goal
milestone) when a PR it merges changes a matching file. Later matching merges join the open one, and none opens within
`every_hours` of the last one closing. Without the table nothing happens (commonroom#53). The digest's `hours_since_last` lists every such
role, a never-run one as `null`.

## Roadmap
- **v2.0:** ledger, usage readers, gate, CLI.
- **v2.1:** dispatcher, worker runner, issue comments, Codex reviewer.
- **v2.2:** dashboard, approvals, the owner's queue, digests and notifications.
- **v2.3:** estimate calibration from the ledger, breaker tuning, more projects.
