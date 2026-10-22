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
python D:/Dev/commonroom/room.py pause "why"     # nothing new starts; running workers finish their job
python D:/Dev/commonroom/room.py pause --hard "why"  # also stop running workers now (their work is saved as WIP)
python D:/Dev/commonroom/room.py breaker reset   # clear a pause or a tripped breaker after reading the diagnosis
python D:/Dev/commonroom/room.py tick            # run the project's [heartbeat] commands (the heartbeat does this)
```

Each project plugs in with a `room.toml` and `.claude/agents/` in its own repo. The heartbeat runs for one project at a time (`wscript.exe D:\Dev\commonroom\heartbeat.vbs <project dir>`, every 15 minutes from Task Scheduler): that project is the host, and Common Room's own dispatcher rides on its allowance. Switching projects means pointing the scheduled task at another folder.

`room tap` is a Claude Code status-line command. It records your Claude rate limits on every response and prints a compact usage line.

The ledger lives at `~/.commonroom/room.sqlite3`.

## Develop
```
python -m unittest discover -s tests -v
```
The room builds its own improvements: this repo is a project with its own `room.toml`, and the heartbeat runs a dispatcher for it as a guest on the allowance of the project it runs for. Agents working here read [AGENTS.md](AGENTS.md) first. In short: work in a worktree under `D:/Dev/commonroom-wt/`, never in the live checkout, and never stop room processes. There is no CI here: the reviewer's tested approval is what merges a PR. See [docs/design.md](docs/design.md), "The room serves itself".
The core uses only the standard library. The optional `/usage` screen reader needs `pywinpty` and `pyte` (`uv pip install pywinpty pyte` into `.venv`).
