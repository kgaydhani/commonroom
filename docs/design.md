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
| Dashboard | `room dashboard` at http://127.0.0.1:8770: one plain page (commonroom#248, `commonroom/page.py`): the week, the gate's and the pie's verdicts, the pie (a row per project and the owner's slice, estimates marked), what waits on the owner, and each project folded (working or resting until, what landed, open breakers). `/api/state` is the raw detail | rebuilt 2026-11-21 |

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

- **Guest on the host's plan (`[share]`).** *Replaced by the registry and beat: every project has its own share and the beat runs them all, with no host (see [design-v3.md](design-v3.md)). The text below is the old design, kept for history.* One ledger, one meter. The host is the project the heartbeat runs for: `heartbeat.cmd` sets `COMMONROOM_HOST` to that folder, and it overrides `[share] host` in any project that has a `[share]` table, so switching projects is a scheduled-task change. `[share] host` is the fallback for commands run by hand, and a project never hosts itself (commonroom#151). The guest reads its budget gate (reserves, pace, burn cap, owner yield) from the host's `room.toml`, so the two can't drift apart. The host's room-wide stops (owner or auditor pause, no_progress) hold the guest too. The guest's model runs take at most `max` (15%) of all projects' autonomous run time per provider over the trailing `hours` (24). The exception is when no other project has used that provider for `idle_minutes` (60): then nobody is waiting on that allowance. The host is never capped by a guest. A guest's progress guarantee charges it only its share of the plan-wide spend.
- **Room workers are not the owner.** Every run records the folder its worker ran in. When a project checks whether the owner is using Claude elsewhere, it ignores those folders, so neither project mistakes the other's workers for the owner.
- **Merging without CI (`[checks] ci = false`).** Housekeeping merges on the newest `REVIEW: APPROVE` posted after the last commit, but only when that approval carries `TESTS: PASS <head sha> <test command>: <result>`. The sha has to match the PR head, because a commit pushed during a review can still predate the review comment. An approval without that line goes back for review, and the review run counts as failed, so the no-output breaker stops a loop. `review:skip` doesn't apply without CI. As for every project, main moves one merge per housekeeping pass, and only a branch that is up to date with main merges (commonroom#19). When the room merges main into a branch, the branch gets a new head, so the merged tree is reviewed and tested again before it lands.
- **Carrying a tested approval (`[merge] carry_tested_approval = true`).** Without CI, when the reviewer's newest verdict is an APPROVE whose `TESTS: PASS` names a commit of the PR, and every commit after that one is a merge of main that had no conflicts, housekeeping runs `[checks] test` on the new head itself: in a temporary detached worktree, under the suite lock `room.py check` takes, stopped after `[checks] max_minutes` (default 10). It posts `[room] TESTS: PASS <head sha> <command>: <result line>` with a line `CARRIES: APPROVE <approval createdAt> TESTED <sha>`, and the merge gate takes that approval for this head, with no reviewer run. A failing or unfinished run is posted as `TESTS: FAIL` or `TESTS: INCOMPLETE` and carries nothing: the PR goes back to review. A conflicted merge, any other commit, a director stamp that can't itself be carried, or `ci = true` carry nothing. One run per pass, none after a merge in that pass, each a ledger row with role `carry` and provider `none`. A run delays the project's other housekeeping by its length. Unset: as before (commonroom#226).
- **Merge train (with `carry_tested_approval`).** When at least two open issue PRs on `main` are approved and tested on their own heads, behind main, not conflicting, not the director's (no `needs:director` on the PR or its issue, no director paths), not claimed or being built, and main isn't red, housekeeping builds a train before the one-at-a-time path: from `origin/main` it merges each PR head in PR-number order with `git merge-tree` and `git commit-tree` (objects only, no checkout touched), leaving out any PR that conflicts with what is before it. With two or more in it, it runs `[checks] test` once on the tip, under the suite lock and `max_minutes`, as a `carry` ledger row whose output lists each member and head, and comments `[room] TESTS: <result> <tip sha> ...` on every member. On PASS it lands them in order in the same pass: for each, it merges the current main into the PR head, checks the tree is the step the tests ran through, pushes that commit to the PR branch (a fast-forward) and squash-merges with `--match-head-commit`, so main ends at the tested tree; a difference or failure stops the train there. A PR left out for conflicting then goes back for a merge-main run. On FAIL or INCOMPLETE nothing merges, and those heads never ride a train again: each goes one at a time, which finds the failing one. Fewer than two members, or carrying off: as before (commonroom#612).
- **Look before building (`[checks] look_before_building = true`).** The dispatcher does not pick a ready `role:builder` issue labelled `kind:feature` unless its body has a line `Scouted: #N` naming a closed issue, or it carries the label `scout:waived`. A held issue gets one `[room] Held by look-before-building` comment saying what is missing, and is skipped. Bugs, infra and unlabelled issues are never held. Unset: every builder issue is picked as before (commonroom#325).
- **Project prompt guidance (`[prompts]`).** Room prompts name only the project's `[checks] test` command. Anything tool-specific comes from the project's room.toml: `[prompts] headless` is appended to the headless note every worker gets, and `[prompts] codex` is the sandbox and tooling note in Codex build and review prompts. An unset key adds nothing (commonroom#45).
- **Where prompt text lives (`[ways] dir`).** Every word the room tells a worker (task prompts, the continuation, headless, trust and test notes, the steer and bulletin reasons, the capability sweep issue) is a template in `commonroom/ways/prompts/<name>.md`, not in `dispatch.py`, which only picks a template and fills its `{{name}}` placeholders. A template's final newline is dropped. The files are read once, at import, so a running dispatcher keeps the text it started with, and a missing file fails the import with its name. A project replaces any of them with `[ways] dir = "<folder in the project>"` in its room.toml: a file there with the same relative name (`<dir>/prompts/build.md`) replaces the room's, read when the dispatcher starts, and may use only the placeholders the room's file uses. Unset, nothing changes. `tests/test_prompt_golden.py` renders every prompt and compares it byte for byte with `tests/golden_prompts/`; a deliberate wording change re-renders them with `ROOM_REGEN_PROMPTS=1` and shows the diff in its PR (commonroom#216).
- **Where the ways-of-working numbers live (`[policy]`).** Review and CI rounds, cooldowns, the stale-claim and resume windows, the room-error backoff, the progress breaker's minimum work time, the mention cap and the run time limit are one table, `commonroom/ways/policy.toml`, each with why it is what it is. It is read once, at import, and a missing file or name fails the import naming it; the old constants (`dispatch.REVIEW_ROUNDS`, `workers.RUN_TIMEOUT` and the rest) hold the table's values. A project overrides a value with the same name under `[policy]` in its room.toml, for its own dispatcher only; unset, nothing changes, and an unknown name fails when the dispatcher starts. `ci_reruns` and `ci_backoff_minutes` can be set per project (commonroom#228), and `progress_min_work_seconds` is room-wide only because it is a breaker's timing. Some values have a floor or ceiling beside them in the table (`{ value = 3600, min = 300, max = 14400 }`); a project's value outside it fails at start naming it, and a change to the table needs the director's stamp (commonroom#230). Budget and gate settings stay in `[budget]`, `[breakers]`, `[parallel]`, `[owner]`, `[share]` and `gate.py` (commonroom#217).
- **Codex scratch folders (`[codex] scratch_env`).** The Codex sandbox writes only inside the worktree. Each environment variable the project lists (gamemirage: `APPDATA`, where Godot writes) points a Codex worker at `<worktree>/.scratch/<name lowercased>`, made before launch and kept out of `git status` by the repo's git exclude. Whatever the project sets, a Codex worker's `TEMP`, `TMP` and `TMPDIR` also point at `<worktree>/.scratch/tmp`, because the sandbox can't write to the shared `commonroom-tmp` folder Claude workers use and `tempfile` there failed with WinError 5 (commonroom#158). Claude workers keep `commonroom-tmp`, and get no other change (commonroom#81).
- **Bulletin watchdog (`[bulletin]`).** The owner's digest is due on issue `issue` by each local `deadlines` time. Once a deadline `D` passes with no comment starting with `@<repo owner>` since the deadline before it (bulletins go out early) and no director run since `D`, `director_trigger` adds `bulletin missed (due HH:MM)`: the director posts the digest and logs that the interactive session's schedules need recreating. It fires at most once per deadline. Unset, nothing changes (commonroom#82).
- **Owner notifications (`[notify]`).** The room acts on GitHub as the owner's own account, so its `@mentions` and assignments never notify the owner. With `ntfy_topic` set (and optionally `ntfy_server`, default `https://ntfy.sh`), housekeeping pushes to the owner's phone: each new open `owner:human` issue (several in one pass go as one notification), each new comment that mentions `@<owner>` (bulletins included; at most 5 a pass, the rest summed up), and a breaker stop at once at high priority. The first pass only sets the comment watermark, so history isn't replayed. A failed push is logged and retried next pass. The owner is `[project] owner`, default the repo's owner. Unset, nothing is sent (commonroom#151).
- **Only trusted logins count (`[project] trusted`).** The repos are public, so anyone can open an issue or PR, or comment. Every board read the dispatcher makes goes through `TrustedBoard`: it keeps only issues and comments whose `author.login` is the owner (agents post as the owner's account, so their comments still count) or a login in `[project] trusted` (a list, e.g. a co-editor; default empty), compared case-insensitively, and drops anything with no author. So a stranger's `REVIEW: APPROVE`, director verdict, comment on an `owner:human` task, `@owner` mention, bulletin or `status:ready` issue does nothing. A PR counts only when its head is a branch of this repository (`isCrossRepository` false, head owner the repo's owner) and its author is trusted: any other open PR is never reviewed, updated, labelled or merged, and housekeeping reports it once, in its log and to the owner's phone. The dashboard's owner queue shows only trusted comments too (commonroom#176).
- **Owner approval (`[goal] approve`).** `"never"` (the default) runs every `status:ready` item. `"item"` starts a builder or scout only on a ready issue that also has `owner:approved`; the room never adds or removes that label, and reviews and merges of open PRs are unaffected. A capability sweep files its issue and waits like any other, one at a time. The digest then says how many ready items wait for the label. Any other value is a config error (commonroom#413).
- **Scheduled role runs (`[schedule.<role>]`).** Housekeeping opens a `role:<role>` + `status:ready` issue in the goal milestone at each `at` local time (`"07:00"` or a list) or every `every_hours`, with `title` and `body` (`{date}` and `{weekday}` are filled in) and any extra `labels`. A first run is due at once. While the last one it opened is still open, none opens. The role needs a `[roles]` entry and an agent file (or is `builder` or `scout`). Unset, nothing is scheduled (commonroom#151).
- **Heartbeat commands (`[heartbeat] commands`).** `room tick`, run by every heartbeat, runs these argv lists in the project folder, in order (`"{python}"` is the room's interpreter), so a per-project job such as a queue runner rides the heartbeat of its own project only. Unset, nothing runs (commonroom#151).
- **Director log (`[director] log_issue`).** The steering trigger names this issue (a number, or `owner/repo#N`) as where the Director log goes. Unset, it names none.
- **Director paths (`[merge] director_paths`).** A PR that changes a file matching one of these globs (commonroom: `gate.py`, `config.py`, `room.toml`) merges only when, on top of the usual approval, a comment starting with `[agent:director]` and holding `REVIEW: APPROVE` follows its last commit and has a line `HEAD: <sha>` that names the PR head (at least 7 characters, a prefix of `headRefOid`), so a commit made before the approval but pushed after it doesn't ride on it. A missing or stale `HEAD:` line means not approved yet, and the trigger text gives the director the sha to post (commonroom#55). The director's verdict doesn't count as the reviewer's. Housekeeping asks for it only once the branch is up to date with main, so the director judges the tree main becomes and the update doesn't void their approval. While such a PR waits only on the director, `director_trigger` names it. A director `REVIEW: CHANGES` after the last commit sends the PR back to its builder, as a reviewer's does, and the trigger stops naming it. Unset, nothing changes (commonroom#49). A PR labelled `needs:director` waits the same way whatever files it touches: the builder adds the label when the change needs lines in another project's `room.toml` or prompts, and the director lands them there before approving (commonroom#65).
- **Where housekeeping runs.** Besides the start of each slot-1 Claude pass, the heartbeat starts `room.py housekeep` for the host project and for Common Room: a pass now and every 3 minutes for 14 minutes, with no job and no gate, so merges and unblocking don't wait behind a long build. A per-project `<name>-housekeep.lock` keeps two passes from running at once, so one merge per pass still holds (commonroom#160).
- **Updating the live checkout.** After merging into the room's own repo, housekeeping runs `git pull --ff-only` in the checkout the room runs from. Without CI (the room runs main's suite itself), it moves only to a main sha whose suite passed in code (`main-suite:<sha>` marked, `main-suite-red:<sha>` not), with `git merge --ff-only <sha>`, never past it to a newer untested main; every pass checks, so the move comes the pass after the suite passes, and a red or untested main holds it with a line that says so (commonroom#396). It never resets or forces: a diverged, dirty-conflicting or non-`main` checkout is left alone and reported. This is safe with room processes running:
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

## The pie: registry, arithmetic and standings (shown, not yet gated on)
The first part of "The pie" in [design-v3.md](design-v3.md) exists and `room pie` shows it, but nothing is gated on it
yet, so every project runs as described above (commonroom#225, commonroom#238).
- **Registry** (`commonroom/registry.py`): `<room home>/projects.toml` lists projects, each with a `path` holding a
  `room.toml`, a `share` of the weekly allowance (0 to 100, summing to at most 100), `work` (`fixed` or `open`), `when`
  (`paced` or `spare`) and `strict`. The owner's own slice is `100 - sum(shares)`, derived. A missing file is an empty
  registry. `room projects` lists it; `add`, `set` and `remove` change it, and refuse a change that breaks a rule.
- **Arithmetic** (`commonroom/pie.py`, pure): an estimate of each project's weekly spend from plan-wide readings and
  run times (growth counts only above the week's running high, because the meter flaps by a point; a fall of more than
  `RESET_DROP` is an early reset; each step is shared by the runs' expected cost inside the gap); each project's line,
  `share × elapsed + share% of weekly_slack`; and whose turn it is: the `paced` project with work furthest under its
  line for its share, else one over its line on share lent by a non-strict project with nothing ready while the
  projects' total spend is under their lines (strict projects' unused share held back), else a `spare` project. A
  share of zero never runs. The owner's own use is the gate's business, not the pie's.
- **Standings** (`commonroom/standings.py`, read-only): for one provider at a given `now`, the weekly window of the
  latest `seven_day` reading (none once it has reset), that window's readings only (another window's reset time is
  left out), every run on the provider in it except runs with no model and open runs whose dispatcher died
  (`liveness.orphaned`), and each registered project's estimated spend, line (the room's default `weekly_slack`) and
  headroom. Runs are matched to a registry entry by the `[project] name` in the `room.toml` at its path. `room pie
  [--json]` prints them per provider with the owner's slice and his estimated own use (plan-wide less every room
  project's estimate), marks every estimate, and marks a reading older than `freshness_seconds` as STALE.
- **Own sessions** (`commonroom/sessions.py`, commonroom#239): on Claude, the owner's own Claude Code sessions count
  as the project they ran in, beside its room runs, as runs with no cost. The reader opens every transcript folder
  under `~/.claude/projects` (each session's file and its `subagents/`), skips files not modified since the window
  began, and books each assistant line by its `cwd`: to the registered project whose folder equals it or holds it
  (real paths, case-insensitive on Windows), or else to the owner's own slice. The folder name can't decide this,
  because `/` and `.` both encode as `-`. The owner's sessions in unregistered folders take part in the time split
  like any run, so a meter step during his work elsewhere stays his and isn't handed to a project that also had a
  session open. Lines of room workers (a run's folder or session id in the ledger) are left out. One session's
  messages less than `GAP` apart are one span. It reads only timestamps, session ids, `cwd` and usage numbers, never
  message text. `room pie` splits each project's estimate into "room" and "own sessions". With no registered project
  it reads nothing. Accepted for now: an own-session span carries no cost, so a meter gap that holds one is split by
  time among all its runs, room runs with known costs included.
- **Registry warnings.** `add`, `set` and `remove` refuse only for the project being changed and for rules of the
  whole registry. Another project's problem, such as a missing folder, is printed as a warning and the change is
  saved.
- **Scheduled registry changes** (commonroom#488). `room projects set <name> --share <points> --at <time>` (also
  `--work`, `--when`, `--strict`) queues fields in `<room home>/projects-pending.json`, leaving `projects.toml`
  untouched. Times are ISO 8601 with a zone, or local `YYYY-MM-DD HH:MM`. Before planning, each beat applies due
  entries in time order through the registry's checks and logs the old and new values. Refused entries are logged
  and removed too; other entries and the beat continue. `room projects` shows the queue with UTC times;
  `room projects set <name> --cancel-pending` drops all pending entries for that project. Queue writes use a short
  lock and atomic replace. With no pending entries, projects behave as before.
- **One-week raises** (commonroom#242). `room pie raise <project> <points> [--from <project>...]` transfers points
  in proportion to the donors' unspent shares; by default donors are the other projects. `--from owner` explicitly
  includes the unassigned slice (`--from "@owner"` works even when a project is named `owner`). Raises cover every provider with a
  current weekly reading; `--provider claude|codex` selects just one. A refusal writes nothing, including when one
  provider cannot fund the raise. `room pie raise --clear [<project>]` removes all raises or just that recipient's.
  If a recipient has donated its raised share onward, clearing just it can be refused to keep every share valid:
  clear the dependent raises first, or clear all.
  Transfers live in `<room home>/pie-raises.json`, separate from `projects.toml` so standing shares are untouched,
  with provider, recipient, donor amounts and `resets_at`. Standings apply the effective shares and lines, which the
  dispatcher reads too; `room pie` and `--json` show the donors and expiry. A scheduled or early reset expires them
  (including a meter drop over `RESET_DROP` with an unchanged reset time); expired entries disappear on the next
  successful raise or clear write. A short-lived write lock and atomic replace protect concurrent commands/readers.

## The handshake: a room folder per project (first cut)
"The handshake" in [design-v3.md](design-v3.md), without `hand` or `share` yet (commonroom#253). It reads the registry
and the ledger and writes only its own folders, so no project's behaviour changes.
- **The folder** (`commonroom/handshake.py`): `<room home>/projects/<name>/`, or the registry's optional `shared`
  folder for that project (a full path, such as a synced drive). It holds `README.md` (from
  `commonroom/ways/handshake/`, rewritten when its template changes), `charter.md` (created once with empty headings
  and never written again), `digest.md` (built in code: the charter, share and mode, who attached, the five newest
  notes, and the last 7 days of runs and landings from the ledger; the cost figure is marked as an estimate) and
  `inbox/` (one new file per note, never edited or deleted; front matter is optional, and a file that is not text is
  still listed by name). An agent with no room reads `digest.md` and adds a file to `inbox/`.
- **Commands** (no model, no GitHub): `room attach [<project>] [--as <who>]` (the project named, or the one whose
  folder or room folder holds the current directory) makes the folder, records the attach as a ledger mark and prints
  the digest. `room note <text>|- [--project] [--from] [--to]`, `room notes [--project] [--since]` and
  `room digest [--project]`. `room digest --hours N` is still the auditor's JSON summary.

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
