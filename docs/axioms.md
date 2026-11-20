# Axioms (proposed, 20 Nov 2026: the owner has not yet confirmed them)

These follow the pattern of gamemirage's `design/axioms.md`. They are the owner's values, drafted here from his words for him to correct. Agents do not change them. Where this list and his words differ, his words win. Each has a check that code can run, so any session can verify the room is on track without trusting another session's account. They would live in `docs/axioms.md`, with a decisions ledger whose every row names its axiom and its test.

| | Axiom | From | Check |
|---|---|---|---|
| A0 | **Beside, never between.** The room never hinders, limits or removes any feature of the tools it works with. It attaches through their own extension points and a command line. Removing it changes nothing else. | Owner, 13 Nov | The plugin's hooks return only added context: a test fails on any block, rewrite, deny rule or taken slot. |
| A1 | **Readable and replaceable.** Everything a model is told by the room comes from a plain file the owner can read, edit or delete. | Owner, 13 Nov | A test fails if core or adapter code contains instruction text. |
| A2 | **The room knows no project.** A project can be anything. It owns "better"; the room only counts what the project declares as "landed". | Owner, 20 Nov | A test fails on any project name, path or login in room code. |
| A3 | **Code holds obligations. Models hold none.** Nothing is forgotten because a conversation moved on. | Owner, 13 Nov | Ledger invariants run every beat, and a test covers each. |
| A4 | **Spend is tied to landing.** Usage with nothing landed stops within a day. | v1 post-mortem | The breaker tests. |
| A5 | **The owner's own use comes first.** An unknown reading is not free capacity. Interactive use is shown, never blocked. | v1 post-mortem; owner, 13 Nov | The gate tests. |
| A6 | **Code decides what runs. Models do the work.** | v2 design | A test fails if the dispatch path calls a model. |
| A7 | **Ways of working change on evidence, without touching the wiring.** | Owner, 20 Nov | A change to a ways-of-working file needs no code change, and names the trial that supports it. |
| A8 | **Each subscription runs only on its owner's machine.** | Vendors' terms | No code path accepts another person's credentials. |
