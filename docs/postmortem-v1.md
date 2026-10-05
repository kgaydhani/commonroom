# Common Room v1: post-mortem

Sources: Kieran's account (2026-10-05) and v1's own database (`D:\Dev\Crypto\.local\room.sqlite3`, covering 23–24 Sept 2026). v1 is not running.

## What it felt like to use
1. **Opaque.** It was hard to tell what it was doing, why, or whether anything was getting closer to done.
2. **No goal.** There was plenty of activity and no destination. Progress wasn't visible because nothing defined it.
3. **Allowance burned with nothing to show.** A 5-hour window was gone minutes after a reset. A whole week's Claude allowance went overnight, and none of it produced anything of value.
4. **It couldn't fix itself.** The in-room director and chat couldn't make the changes Kieran asked for. Real fixes happened in an outside builder chat (ChatGPT), which had different permissions from the room's own builder, so he kept going back and forth.
5. **The wrong relationship with the owner.** Kieran wanted two things:
   - To be treated as a team member who gets assigned work ("I need this art by this date").
   - To be told plainly when a suggestion is wrong, instead of having it either obeyed or quietly ignored.
6. **Human-scaled estimates.** The models estimated time as if a person were typing, and judged feasibility by Kieran's own skills instead of by what the agents themselves can do.

## What the data shows
| Measure | Value |
|---|---|
| Automated model turns | 193 |
| …of which planning | 84 (44%) |
| Director decisions | 27 develop, 25 research, 21 wait, 3 paper experiments |
| Backlog | 62 tasks: 6 deployed, 8 awaiting validation, 13 deferred, 39 queued |
| Worst task | attempted 18 times |
| Opus "proposal" (developer) runs | 24, of which 19 ended with no usable result |
| Paper-trading sessions (the actual objective) | 3 sessions, 1 order submitted (cancelled) |

How the workers ran: they had read-only tools and returned JSON find-and-replace edits that the coordinator applied, so they never ran the code they changed. Claude's usage was read by driving `/usage` in a hidden terminal and parsing the screen.

## Root causes
- **The planner was the loop.** A model decided what to do next on every cycle, so deciding became the main activity. Nothing required a decision to produce an artifact.
- **Nothing limited intake.** Any turn could add todos, and nothing capped, pruned or ranked them against a goal.
- **Workers couldn't run anything.** Edits were proposed blind, and validation failures came back as retries instead of fixes made by an agent that could run the tests.
- **Spend was never compared with output.** The gates asked "is there allowance left?" and never "did the last spend produce anything?". So it spent down to the floor.
- **Two builders, two permission models.** The room's builder was sandboxed and fenced off from protected files, so the real fixes happened outside the room. The room couldn't repair itself.
- **The owner had no defined role.** Kieran was either approving hourly notices or a "junior colleague" whose messages were suggestions. He was never an assignee with deliverables, and never a decision-maker with a queue.

## Worth keeping
- Reading usage without spending any, a reserve floor, and "an unknown reading is not free capacity".
- Treating owner messages as input to judge on its merits, with explicit `/admin`-style commands that carry authority.
- Cross-provider review, with Codex and Claude checking each other.
- A mandate file included in every run.
- Durable state, and resuming after restarts.
- Honest records: never claiming work happened that didn't.
