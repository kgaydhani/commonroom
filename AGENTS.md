# Common Room: rules for every agent

Every agent working on this repo reads this first: Claude Code (through `CLAUDE.md`), Codex, and Common Room workers.

## The project
- Common Room is the Python orchestrator that runs the owner's AI studio: the ledger, usage gate, breakers, dispatcher, workers and dashboard. It is a product of its own: each project plugs in with a `room.toml` and `.claude/agents/` in its own repo, and the heartbeat runs one project as the host at a time. How it works: [docs/design.md](docs/design.md). Why v1 failed: [docs/postmortem-v1.md](docs/postmortem-v1.md).
- The room improves itself through this repo (commonroom#31). Its own dispatcher builds, reviews and merges issues in the milestone named in `room.toml` `[goal]`, as a guest on the host project's allowance (`[share]`): the project the heartbeat runs for.
- The owner is Kieran (GitHub `kgaydhani`). The director (Claude Opus) owns the roadmap, reviews and merges.

## The live room is running. Don't touch it.
- **`D:/Dev/commonroom` is the live checkout.** The heartbeat, every dispatcher and the dashboard run from it. Never edit, commit, check out, reset or stash in it. Read it if you need to.
- **Work in a worktree under `D:/Dev/commonroom-wt/`.** Room workers already start in one (`D:/Dev/commonroom-wt/room/<branch>`). If you need another, make it with `git -C D:/Dev/commonroom worktree add D:/Dev/commonroom-wt/<name> -b <branch> origin/main`, and remove it when you're done.
- **Never stop, kill or restart room processes**: dispatchers, the dashboard, services, the heartbeat, moonshots, or any `python` process. That means no `taskkill`, no `room.py stop`, no `room.py pause`, no `room.py breaker reset`, and no Task Scheduler changes. A change that needs a restart says so in its PR, and the director activates it from an interactive session with the owner. That session is the only exception to this rule.
- **Never use the live ledger in tests.** It's `~/.commonroom/room.sqlite3`. Tests build a `Ledger` in a temporary folder, as `tests/` already does.
- The room updates its own live checkout with `git pull --ff-only` after it merges a PR here. Nobody else does it, except the director by hand.

## How work flows
- Every change belongs to a GitHub issue in the goal milestone. No issue, no work.
- Branch `i<issue>-<slug>`. Make small commits and small PRs (aim for under about 300 changed lines of code), each with tests for the behaviour it adds or fixes. The PR body says `Closes #<issue>`, lists each acceptance criterion with how you verified it, and lists what you couldn't verify.
- **A change that needs lines in another project's `room.toml` or prompts** (for example a project's `[prompts]`) would break that project the moment it merges. Add the `needs:director` label to the PR and put the exact lines in its body. The PR then waits for the director's `[agent:director] REVIEW: APPROVE`, and the director opens the companion PR in that project before approving.
- **Tests:** `python -m unittest discover -s tests`, run from the worktree root. It must pass before you push. The core uses only the standard library.
- **No CI.** Nothing on GitHub runs the tests. The reviewer runs them on the PR head and states the result in the review, and housekeeping merges on that approval, one PR per pass.
- **Blocked:** comment on the issue with exactly what's missing, then stop. Don't retry the same approach.
- Stay inside the issue's scope. If you find other work, open an issue for it in this repo instead of doing it.

## Engineering rules
- Python 3.11+, standard library only in the core (`pywinpty` and `pyte` are optional extras for the `/usage` reader).
- Dispatch, pacing and gates are plain deterministic code. Models do the work; code decides what runs.
- Import room modules at the top of a module, never inside a function. Running dispatchers keep the code they loaded at start, and the room pulls new code into the live checkout underneath them.
- Don't change budget or gate thresholds, reserves, caps or breakers without an issue that asks for it. Those are the director's call.
- Anything project-specific lives in that project's `room.toml` or prompts, not in room code. No project's name, path or login is hard-coded here. A change to the room must not change any project's behaviour unless its issue says so. Test that.
- **Line endings are LF.** The repo stores LF, and your working tree may show CRLF because of `core.autocrlf`. When you rewrite a file with a script, write LF.
- Never force-push. Never push to `main`.

## Machine and tools
See `D:/Dev/gamemirage/docs/environment.md` (this PC's tools and storage rules; they apply to every project). Use the `gh` CLI for GitHub. Use `--repo kgaydhani/commonroom` on every `gh` call, because the room works on several repos.
