# A local board for projects that are not code: beads, git-bug, or our own stdlib files? (#450)

Scouted 2026-11-23 for slice 5 of `docs/design-v3.md`, "Projects that are not code: a local ledger backend". Read-only:
nothing was downloaded or installed, and no account was used. Every finding below comes from the projects' own READMEs,
CLI references and GitHub release data, read on 2026-11-23. None of the tools was run on this PC, so the claims about
how they behave on Windows are their docs' claims, not tested.

**Short answer.** Build our own: a standard-library `LocalBoard` that keeps one folder per item, writes every change as
a new file and never deletes anything, as #454 already describes. Every existing tool fails at least one hard
constraint. beads, the strongest candidate, has a single writer in its serverless mode. It needs a `dolt sql-server`
before more than one process can write, and the room always has more than one writer (dispatchers, workers, the
owner's session). git-bug needs a git repository and is GPL-3.0. Backlog.md and ticket keep each item in one Markdown
file that is rewritten in place. All four need a download and a runtime the core doesn't have, and only Backlog.md has
the integer numbers the room uses everywhere.

## Sources

| Ref | Source | Date or version |
|---|---|---|
| B1 | beads README, https://github.com/steveyegge/beads (the repo now also lives at `gastownhall/beads`) | `main`, read 2026-11-23. MIT. Latest stable `v1.3.4` (2026-11-04), latest pre-release `v1.4.0-rc.4` (2026-11-20) with `windows_amd64.zip` assets |
| B2 | beads CLI reference, https://github.com/steveyegge/beads/blob/main/docs/CLI_REFERENCE.md | `main`, read 2026-11-23 |
| B3 | beads install guide, https://github.com/steveyegge/beads/blob/main/docs/getting-started/installation.md | `main`, read 2026-11-23 |
| G1 | git-bug README, https://github.com/git-bug/git-bug | `main`, read 2026-11-23. GPL-3.0. Latest release `v0.12.0` (2026-11-07) with `git-bug_windows_amd64.exe` |
| G2 | git-bug generated CLI docs, https://github.com/git-bug/git-bug/tree/master/doc/md (`git-bug_bug.md`, `git-bug_bug_show.md`, `git-bug_bug_new.md`, `git-bug_bug_comment_new.md`) | read 2026-11-23 |
| K1 | Backlog.md README, https://github.com/MrLesk/Backlog.md | `v1.57.4` (2026-11-22). MIT |
| T1 | ticket (`tk`) README, https://github.com/wedow/ticket | `v0.3.2` (2026-02-03). MIT |
| R1 | `GitHubBoard`, `commonroom/dispatch.py` lines 416 to 610 of this branch; `board.issues()`, `commonroom/board.py` | `main` at `0a233a8` |
| R2 | `docs/design-v3.md` line 118, "Ledger": "Agents may add items and change state. They never rewrite or delete." | `main` at `0a233a8` |

## 1. Is there a free, local, file- or sqlite-backed tracker that agents already use?

There are several, and agents do use them. None of them fits the room as it stands.

| | beads (`bd`) | git-bug | Backlog.md | ticket (`tk`) |
|---|---|---|---|---|
| Licence | MIT (B1) | GPL-3.0-or-later (G1) | MIT (K1) | MIT (T1) |
| Storage | Dolt database in `.beads/`. "Embedded (default) … Dolt runs in-process … **single writer**". More than one writer needs "an external `dolt sql-server`" (B1). `.beads/issues.jsonl` is "an export … not the source of truth" (B1) | Git objects inside the repository's `.git`, "no files are added in your project" (G1) | One Markdown file per task in `backlog/` (K1) | One Markdown file with YAML front matter per ticket in `.tickets/` (T1) |
| Install on Windows | A prebuilt `windows_amd64.zip` from Releases, npm `@beads/bd`, `install.ps1`, or winget (B1). `go install` needs Go 1.24+, and the embedded mode needs CGO with MinGW (B3). There are antivirus false-positive notes for Windows (B1) | A prebuilt `git-bug_windows_amd64.exe` (G1 release assets) | `npm i -g backlog.md` or Bun (K1). Node is on this PC | A bash script. It needs bash and coreutils, plus `jq` for `query` (T1). Git Bash is on this PC; `jq` was not checked |
| Daemon or server | Not in embedded mode. Needs `dolt sql-server` for more than one writer (B1) | None. `webui` is optional (G1) | None | None |
| Outside a git repo | Yes: "Beads works without git", using `BEADS_DIR` (B1) | **No**: "you only need your git repository" (G1) | Yes, with `backlog init --no-git` (K1) | Yes, plain files (T1) |
| IDs | Hash strings, `bd-a1b2` and `bd-a3f8.1` (B1) | Hashes (G1) | `TASK-1` (K1): the only one with integers | Ticket IDs used as file names (T1) |
| JSON from the CLI | A global `--json` flag on every command (B2 "Global flags"), with `bd comments <id> --json` and `bd list`/`bd show --json`. `bd schema` prints the JSON Schema of the output (B2) | `git bug` (list) and `bug show` take `--format json`. `bug new` and `comment new` take `-m` and `--non-interactive` but print text (G2) | `--json` on `task list`, `task view` and `search`. Writes print text (K1) | `query` goes through `jq`. The rest prints text (T1) |
| Can delete or rewrite | `bd delete` and `bd purge` exist (B2). Dolt keeps the history | `bug rm`, `wipe` and `comment edit` exist (G2) | `task delete` exists, and comments are written into the task file (`task edit --comment`, K1) | Status edits rewrite the ticket file |

Gas Town uses beads, and beads has the most agent-facing features: `--json` everywhere, `--actor`, `--readonly` for
worker sandboxes, and an atomic claim (B1, B2).

## 2. Could the room drive one through a thin adapter with `GitHubBoard`'s methods?

**The verbs map. The data model does not.** Here is each method in `GitHubBoard` (R1) next to its nearest beads
command (B2). beads maps best, so the gaps below are a floor for the other three tools.

| `GitHubBoard` method | beads | Gap |
|---|---|---|
| `open_issues(milestone)`, `all_issues()` | `bd list --json` with a status filter | No milestones. They would need a `milestone:<title>` label or metadata, and the adapter would rebuild the `{"title": …}` dict |
| `create_issue(title, body, labels, milestone, assignees) -> int` | `bd create … --json` | **Returns `bd-a1b2`, not an int** |
| `comment`, `comment_as_director`, `issue_comments(n, since)` | `bd comment`, `bd comments <id> --json` | The `since` filter runs on the client. The author is `--actor`, not a login |
| `set_labels(add, remove)`, `ensure_label` | `bd label add/remove`. Labels are free-form, so `ensure_label` does nothing | None |
| `close_issue`, `close_not_planned`, `issue_state` | `bd close --reason`, `bd show --json` | beads has more states (open, in_progress, blocked, closed …), which fold to `OPEN`/`CLOSED` |
| `edit_issue(title, body)` | `bd update --title --description` | It rewrites the item, though Dolt keeps the history |
| `set_milestone` | none | Only as a label (see above) |
| PR, run and check methods (`open_prs`, `pr_detail`, `merge` …) | none | They answer "none" on any local board (#455 already says so) |

What would not map:
- **Integer numbers.** The room uses `issue["number"]` as an int for its `i<n>-slug` branch names, for `Scout: #N`,
  for `Depends on #N` and for the ledger's rows. With beads, git-bug or ticket, the adapter would have to keep an
  int-to-ID table in a file of its own. That file is the core of our own board anyway, and it brings back the
  two-writer problem the table's own writes would need to solve.
- **Several writers with no server.** Dispatchers, workers, `room claim` (#456) and the owner write to one project's
  board at once. Embedded beads allows one writer (B1). Backlog.md and ticket rewrite one file per item, so two
  writers on the same item lose an update. git-bug needs a git repository, which a pixel-art project may not have.
- **Never rewrite or delete (R2).** Every tool can delete (B2, G2, K1). The adapter could simply never call those
  commands, but nothing would stop an agent with a shell from calling them.
- **The shapes `board.issues()` returns** (`labels` as `[{"name"}]`, `author` and `assignees` as `{"login"}`,
  `state` as `OPEN`/`CLOSED`, `updatedAt` in ISO format) need an adapter in every case. That part is easy.

## 3. Recommendation

**Build `commonroom/localboard.py` in the standard library, as #454 specifies: one folder per item named by its
integer number and claimed with `os.mkdir`, one new JSON file per comment or change (written as `*.tmp`, then renamed
to a name that doesn't exist yet), state read by replaying those files in order, and no delete call. Adopt no
external tool.**

Why:
1. The core is standard library only (AGENTS.md). Any tool here is an extra binary, which is a download the owner
   must approve and a runtime a buyer must install at slice 7 (packaging). git-bug's GPL-3.0 would also follow it into
   a bundle.
2. The constraints that matter (several writers with no server, nothing rewritten or deleted, integer numbers) are
   exactly where every tool falls short, while all four handle the easy part (JSON lists and labels). An adapter
   would cost about as much as the board itself and still miss those constraints.
3. The board is small. It needs about 11 methods over files, which fits #454's budget of under 280 lines.
   `os.mkdir` and `os.rename` onto a name that doesn't exist both fail on Windows when the target already exists,
   so they give atomic claims with no locks.

What would change this: if beads made its embedded mode safe for several writers, it would be worth an optional
adapter for owners who already use it. `bd import` reads JSONL, so an export from our board to beads stays possible
later without adopting it now.

### Notes for the #454 builder (suggestions, not requirements)
- Layout: `<folder>/board/items/<n>/item.json`, written once at create time, plus
  `<folder>/board/items/<n>/events/<UTC stamp>-<pid>-<random>.json`. Each event is `comment`, `labels`, `state`,
  `edit` or `milestone`. Read the folder and replay the events in name order.
- Numbers: try `os.mkdir(items/<max+1>)` and move to the next number on `FileExistsError`. Two boards on one folder
  then never share a number.
- Readers skip `*.tmp` and any file that fails to parse, so a write still in progress is never read half-done.
- `edit_issue` adds an `edit` event, so the old title and body stay on disk (R2).

## Experiment (pass/fail)

This is #454's own acceptance, sharpened:
- **Pass** when two processes, each with its own `LocalBoard` on one temporary folder, create 50 items each and
  comment on the same item in turns. The test must then find 100 distinct numbers, see every comment from both
  processes on that item, and find that no file present at the midpoint changed its hash by the end (nothing was
  rewritten or deleted). The full suite must also still pass.
- **Fail** on any repeated number, any lost comment, or any changed or missing file. In that case, use a lock file
  (`os.open` with `O_CREAT | O_EXCL`) for number allocation before considering beads' server mode.

## Follow-up issues

- #454 (Local board, part 1): **unchanged in scope.** The scout recommends the stdlib board that #454 already
  describes. The notes above are optional guidance.
- #455 (part 2, `board = "local"`): unchanged.
- #456 (part 3, `room items` and `room claim`): unchanged. `claimed:<name>` is a plain label event on the local board.
- No new suggestion issues: the work is already filed as #454 to #456.
