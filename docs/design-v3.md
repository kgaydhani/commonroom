# Common Room v3: design (draft for discussion)

Draft of 20 Nov 2026. Nothing here is built or agreed. It follows the owner's vision as stated on 13 and 20 Nov, research done on 20 Nov, and the room's own ledger. [design.md](design.md) still describes what runs today.

## What it is for

Weekly subscription allowance expires unused, and one person has more projects than attention. Common Room turns the allowance into progress in proportion to how much the owner cares about each project. A project attaches with a goal and a share of the week. The room keeps it moving between conversations, across Claude and Codex, and keeps track of everything it owes.

It is for people who already use a coding agent every day and still work one conversation at a time. They should get standing projects without building the machinery themselves.

## What already exists (researched 20 Nov 2026)

| Product | What it does well | What it lacks |
|---|---|---|
| Claude Code Projects (public beta) | One coordinator conversation that starts and tracks worker threads, with an overview of what is working and what waits on you. The coordinator reads recent messages and memory files, not its whole history. | No budget: a thread limit "isn't a hard cap". Claude only. One user, not shareable. Idle threads are resolved by timeout after a week. |
| Codex app | Schedules and event triggers on background worktrees. | No usage pacing found. OpenAI only. |
| Orca, Conductor, claude-squad | Many agents in parallel worktrees. Orca shows limits used. | No per-project budget. No ledger of what is owed. |
| Gas Town and beads | An issue graph in git as the agents' memory. | A permanent "Mayor" model session. One user reported about $3,000 for a week. |
| ccusage and similar | Read the subscription windows. | Monitoring only. |

Nobody ships these four things:

1. A share of the weekly allowance per project, enforced in code, across two vendors.
2. One ledger of obligations that both vendors' workers use.
3. A defined way to absorb a change of direction, so old work is finished, parked or closed and never just left.
4. Local dispatch where code decides what runs, for several standing projects at once.

So v3 builds those four and does not compete on the coordinator chat or the thread view, which the vendors now ship and will keep improving. A user can keep any chat surface they like, including Claude Code Projects, and run Common Room beside it.

## What our own ledger shows

| Project | Merges | Worker minutes per merge | Review runs per merge | Director's share of Claude cost |
|---|---|---|---|---|
| commonroom | 66 | 22 | 1.4 | 35% |
| gamemirage | 175 | 67 | 1.5 | 6% |
| magazine | 24 | 302 | 9.7 | 32% |

The same room, the same code and the same models produced a fourteen-fold spread. The magazine's cost came from what each run had to read (about 12,500 words of mandatory reading), a 23-minute test suite, and reviews that bounced. The director is a real cost, but briefs and the review loop mattered more. Codex cost is not recorded, so the cost column covers Claude only.

## Axioms (proposed)

The proposed axioms A0 to A8 now live in [axioms.md](axioms.md).
Each one has a row in the decisions ledger, [decisions.csv](decisions.csv), which `tests/test_axioms.py` checks.

## Structure: three rings

The code is split by how often each part has a reason to change. Today it is not split: 114 of the 157 commits on main touched `dispatch.py`, which holds 24 policy constants and 88 lines of prompt text beside the git and GitHub plumbing. Of the 77 changes since 9 Oct, 69 touched code and 8 touched only prompts, config or docs. So improving how the agents work means editing the riskiest file, and builds queue behind each other on it.

| Ring | Changes | Contains |
|---|---|---|
| **Core** | Rarely, and only with the owner's or director's stamp | The store, the pie and gate arithmetic, the steward's loop, the contracts between modules. It knows no vendor, no project and no instruction text. |
| **Adapters** | When the outside world changes | One per vendor (launch, resume, read usage, parse events), one per ledger backend (GitHub, local), one per workspace kind (git worktree), and the surfaces (command line, plugin, web page). |
| **Ways of working** | Constantly | Files, not code: roles, brief and review protocols, prompt templates, the routing table, thresholds, cadences and triggers. |

The owner's wiring questions map onto modules like this:

| Question | Module | The fixed part |
|---|---|---|
| How agents are connected | Runner and vendor adapters | Start a job, stream its events, stop it, resume it |
| How session context is managed | Runner | The mechanism to start fresh or resume. When to do which is a way of working. |
| How usage is monitored | Meters | One observation shape: provider, window, percent used, reset time, source |
| How things are stored and reached | Store and the room API | Every surface and every agent goes through one API. Nothing else reads the database. |
| Whether git is used | Workspace adapter | A place for a worker to work, and a way to keep what it made |
| How different models communicate | Ledger | They never talk to each other. They read and write the ledger, which is the one channel. |
| What connecting means | Attach | Register the session, load the files, book the usage |
| How the interface is built | Surface | It reads the room API and holds no state of its own |

Each module gets a one-page contract, contract tests (as `tests/test_board_contract.py` does for the board today) and an `area:` label, so builds on different modules run in parallel. A session working on one module needs the axioms and that one contract, and nothing else.

The wiring will still change when a vendor changes something. That is why vendor detail sits in adapters, and why "fixed" means the contracts between modules, which should not need to move.

## How the room improves itself

Common Room is already its own project: its dispatcher has merged 66 of its own pull requests. Two things are missing for it to steer its own improvement.

**Its direction is not written where it can check it.** The axioms, the module contracts and this design fill that gap. A worker can then be told which axiom its task serves, and a reviewer can check the change against a contract.

**It cannot tell whether a change to a way of working helped.** So:

- The room's own measure of "better" is allowance per landing, review runs per landing and wasted runs, across every project it serves.
- Each way of working is a versioned file. A trial is a variant of one file, the share of runs that use it, the measure, and the rule for stopping, all recorded before it starts.
- The core assigns variants and records which one each run used. Code computes the result. The director adopts or reverts.
- One trial per area at a time. At about three landings a day a trial takes a week or more, and only large effects will show. The first candidates are the ones with the largest expected effect: reviewers fixing small findings, resuming a session for review changes, and running the tests in code before any reviewer starts.
- Scouts propose trials. They do not propose rules.

**A core change is the one thing that can break the room building it.** It needs a stamp, the contract tests, and a dry-run beat against a copy of the ledger before the live checkout moves. If the first beat after an update fails its health check, the room rolls back, as v1 did.

## The middle layer

This is the part between the owner's chat and the workers. In v2 one long model session did four jobs. v3 separates them.

| Job | Who does it | How often |
|---|---|---|
| Hold every obligation | The ledger, owned by code | Always |
| Keep on top of things: reconcile, sweep, build the digest, apply the pie, dispatch | The steward: plain code | Every beat |
| Turn a goal or an event into briefs | The planner: a fresh model run that receives the digest and writes only ledger items | On events |
| Speak for the owner | The desk: any interactive session, of any vendor | When the owner is there |

**Ledger.** One record per piece of work: what it is, why, what done looks like, state, holder, and the message or decision it came from. Agents may add items and change state. They never rewrite or delete. Closing an item needs a result from a check or a reviewer. GitHub issues are the first backend, as today. A local backend comes later for projects that are not code.

**Steward.** Each beat it compares the ledger with reality (runs, branches, worktrees, pull requests) and lists anything stale for a decision. It compiles the digest: goal, open items by state, what landed, what waits on the owner, allowance left. The planner, a desk on arrival and another person's agent all read that one digest.

**Planner.** It runs when the owner gives a direction, the ready queue runs low, an item is blocked, or a goal is met, and once a day at most otherwise. It never keeps a session between runs. Routine triggers use a cheaper model. Anthropic's own harness study ran the planner once per build at 0.4% of the cost, and their compiler project used no orchestrating agent at all, only lock files.

**Desk.** A session that attaches reads the digest first. Its one duty is to write the owner's decisions to the ledger before the conversation moves on. With the owner present it also writes the briefs, since it holds the richest context. If the owner changes the subject, nothing is lost, because the desk held nothing.

**Change of direction.** The desk records a direction entry. From then on every open item is "unreconciled" until it is marked keep, park or cancel with a reason. The steward holds unreconciled items and wakes the planner to finish the pass if the desk did not. The invariant is checkable in code: no open item is older than the latest direction without a verdict.

**How much the owner decides** is one setting per project: the planner's new items need the owner's approval never, for each plan, or for each item. That covers the person who plans everything first and the person who wants it to build and report back. An owner who wants to do some of the work claims an item, and the room leaves it alone.

**Workers.** Briefs are self-contained: the objective and why, observable checks for done, boundaries, files to read. They do not prescribe how. Workers start fresh by default. A session is resumed only to answer review findings while its cache is warm. Reviewers always start clean, and fix small findings themselves.

## The pie

One file lists the projects, each with a folder, a share and a mode. One beat serves them all, which replaces the host and guest arrangement.

- **Budget.** The owner's reserve is the ad hoc slice. The rest is divided by share.
- **Spend.** Each run's cost is estimated from its tokens and converted to a percentage of the plan with a factor measured from the meter. Per-project meters are therefore fuzzy, and the interface says so.
- **Pace.** A project may be at most its share of the week so far, plus slack. Among projects with ready work and under their line, the one furthest below goes next.
- **Idle share** is lent to projects with work, unless a project is marked strict.
- **Modes.** A share of zero is off. `fixed` runs only scheduled and explicitly added work. `open` lets the planner add work. `spare` runs only in allowance nobody else wants.
- **Interactive sessions** attached to a project are booked to it and shown. They are never blocked.

## What a project declares

Its goal, its share and mode, how much the owner approves, and what "landed" means. The default is a merge. Otherwise the project supplies a command that lists what has landed since a given time, in the same way it supplies its test command today. The no-output breaker and the progress guarantee then work for a project that never merges anything.

## Attaching a session

Claude Code and Codex now have nearly the same hooks, both read `AGENTS.md`, and both load skill folders. One small plugin serves both.

- A session-start hook registers the session with the room and adds one short paragraph. In a project folder it points at the digest. Elsewhere it says when to offer attaching: work that must outlive the conversation.
- The session's own model makes the offer, so there is no extra model call.
- On yes, the session runs `room attach`, says which files it loaded, and asks about the pie, with "not sure yet" as an answer.
- Everything else is the `room` command (`decide`, `note`, `items`, `claim`, `digest`, `pie`), which any agent with a shell can use.

Usage is read from the status line where the owner has set one, from worker streams, from Codex's app-server, and from a small optional mod. The hidden-terminal `/usage` reader becomes a fallback that is off by default.

## The interface

A local web page, as now, with these parts:

- **Header:** the pie, with each project's fuzzy meter for today and this week.
- **A tab per active project:** working now, landed recently, waiting on you, and the trail.
- **The trail:** each owner message, the decisions and items it became, the runs those caused, and what landed. This is how the owner sees their words being turned into work. It needs only the origin recorded on each item.
- **A message box with two destinations.** "To the project" writes a note to the ledger and always works. "To my open session" passes the text to a live attached session through the optional mod, on Claude only.
- **Settings:** the same plain file, editable by sliders, by hand, or by a small cheap-model session whose only tools are the `room config` commands. It shows the change before applying it.

A true two-way mirror of a live Claude session is not offered. The docs give no supported way to attach a second client, and two writers on one session lose messages.

## Sharing between people

Each person runs their own room on their own machine with their own login. Subscriptions never cross machines: the vendors' terms forbid it. A shared project shares its ledger. A partner's room can hold that project at a share of zero in `fixed` mode, so it pulls and rebuilds the digest in code on a timer at no model cost, and their agent reads it at session start. What they or their agent want to pass on goes in as a note.

## Risks

- **Vendors move.** Projects may gain budgets or sharing. Building only the four missing things limits the loss.
- **`--bare` is due to become the default for `claude -p`**, which would drop the subscription login and all hooks. The room must pin the non-bare behaviour and watch for the change.
- **Plan limits "assume ordinary, individual usage".** The room paces well under the limits and keeps a reserve, but round-the-clock dispatch is a grey area.
- **The mod interface changes almost every release.** Nothing essential depends on it.
- **Windows only today**, and the repository has no licence file.

## Build order

Each slice is a handful of small pull requests through the room's own process.

0. **Foundations for self-building.** No behaviour changes.
   - The axioms file and the decisions ledger, with a test per axiom.
   - Move prompt text and policy constants out of `dispatch.py` into ways-of-working files, with the existing dispatcher tests as the guard.
   - Module contracts and `area:` labels.
   - Metrics per landing.
   - The trial mechanism.
1. **The pie:** project list, one beat, per-project pace, fuzzy meters, declared "landed". About 8 pull requests.
2. **The middle layer:** ledger invariants, the steward's sweep and digest, the event-driven planner, the direction pass. Metrics per merge land here too, so later changes can be judged.
3. **The desk:** the plugin and hooks for both vendors, the `room` desk commands, booking of interactive usage.
4. **The interface and settings.**
5. **Projects that are not code:** a local ledger backend. The pixel artist is the test.
6. **Sharing with a second person.**
7. **Packaging:** installer, other platforms, licence, a bank of default roles and protocols.

Slice 1 is proved when three projects run for two weeks at different shares without the owner pausing the room.

## Open questions for the owner

0. Are the nine axioms yours? Which are wrong, missing or not really axioms?
1. Is lending idle share the right default?
2. GitHub issues first and a local ledger at slice 5: acceptable, or do non-code projects come sooner?
3. Is it acceptable that the room does not try to be the chat, and sits beside Claude Code Projects or any other surface?
4. Which three projects prove slice 1?

## Sources

- Claude Code docs: `claude-projects`, `hooks`, `statusline`, `plugins/mods`, `headless`, `cross-session-messaging`, `channels`, `prompt-caching`, `legal-and-compliance` under https://code.claude.com/docs/en/
- Codex docs: non-interactive mode, app-server, hooks, AGENTS.md, skills under https://learn.chatgpt.com/docs/
- Anthropic engineering: [multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), [effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [harness design](https://www.anthropic.com/engineering/harness-design-long-running-apps), [building a C compiler](https://www.anthropic.com/engineering/building-c-compiler)
- OpenAI: [ExecPlans](https://developers.openai.com/cookbook/articles/codex_exec_plans)
- Cognition: [Don't build multi-agents](https://cognition.com/blog/dont-build-multi-agents), [Multi-agents working](https://cognition.com/blog/multi-agents-working)
- Gas Town: [README](https://raw.githubusercontent.com/gastownhall/gastown/main/README.md), [a week in Gas Town](http://www.dolthub.com/blog/2026-03-24-a-week-in-gas-town/)
- The room's ledger, read on 20 Nov 2026, and the magazine's note "Efficient agent teams" (30 Oct 2026).
