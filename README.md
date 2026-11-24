# Common Room

Weekly subscription allowance expires unused, and one person has more projects than attention. Common Room turns the allowance into progress in proportion to how much you care about each project: a project attaches with a goal and a share of the week, and the room keeps it moving between conversations, across Claude and Codex, and keeps track of everything it owes.

It is for people who already use a coding agent every day and still work one conversation at a time.

- Why it exists, and what went wrong with v1: [docs/postmortem-v1.md](docs/postmortem-v1.md)
- How it works: [docs/design.md](docs/design.md) and [docs/design-v3.md](docs/design-v3.md)

## Use
Each project plugs in with a `room.toml` and `.claude/agents/` in its own repo. Register it once, with its share of the week (the shares are the pie):

```
python D:/Dev/commonroom/room.py projects add myproject D:/path/to/myproject --share 30
```

The beat runs every registered project. Every 15 minutes the scheduled heartbeat runs `room beat`: one pass over the registry that reads the meters, applies scheduled changes, and starts work for each project whose share, gate and breakers allow it. Adding, changing or removing a project takes effect on the next beat. There is no host project and nothing to repoint.

The commands you run day to day:

```
python D:/Dev/commonroom/room.py doctor                      # read-only first-run checks: tools, gh login, registry, each project's room.toml and roles
python D:/Dev/commonroom/room.py init PATH [--goal "milestone"]   # write a starter room.toml and .claude/agents/ files into PATH (never overwrites; then run the `projects add` line it prints)
python D:/Dev/commonroom/room.py projects                    # list the registered projects and their shares
python D:/Dev/commonroom/room.py projects set myproject --share 40  # change a share or mode
python D:/Dev/commonroom/room.py pie                         # each project's effective share and weekly standing
python D:/Dev/commonroom/room.py status                      # goal, your queue, usage, runs, breakers
python D:/Dev/commonroom/room.py usage                       # meter readings and gate verdicts
python D:/Dev/commonroom/room.py attach myproject             # join a project and print its digest
python D:/Dev/commonroom/room.py note myproject "text"       # leave the project a note in its inbox
python D:/Dev/commonroom/room.py digest myproject            # rebuild and print the project's digest
python D:/Dev/commonroom/room.py items myproject             # the project's open items on its board
python D:/Dev/commonroom/room.py beat                        # one heartbeat over every registered project
python D:/Dev/commonroom/room.py pause "why"                 # nothing new starts; running workers finish their job
python D:/Dev/commonroom/room.py breaker reset               # clear a pause or a tripped breaker after reading the diagnosis
```

`room tap` is a Claude Code status-line command. It records your Claude rate limits on every response and prints a compact usage line.

The ledger lives at `~/.commonroom/room.sqlite3`.

## Develop
```
python -m unittest discover -s tests -v
```
The room builds its own improvements: this repo is a project with its own `room.toml`, and the heartbeat runs a dispatcher for it as a guest on the allowance of the project it runs for. Agents working here read [AGENTS.md](AGENTS.md) first. In short: work in a worktree under `D:/Dev/commonroom-wt/`, never in the live checkout, and never stop room processes. There is no CI here: the reviewer's tested approval is what merges a PR. See [docs/design.md](docs/design.md), "The room serves itself".
The core uses only the standard library. The optional `/usage` screen reader needs `pywinpty` and `pyte` (`uv pip install pywinpty pyte` into `.venv`).
