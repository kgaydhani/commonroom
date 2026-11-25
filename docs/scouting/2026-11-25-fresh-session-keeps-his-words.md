# A fresh session that keeps what he said, and whether the person file is the right tool

Scout report for commonroom#626, 25 Nov 2026. Research only: it changes no number and no code.

Every claim carries one of four marks:

- **[doc 25 Nov]**: read in vendor documentation or vendor source on 25 Nov 2026.
- **[study]**: from a published study or report, linked. *Vendor claim* is added where a vendor describes its own product.
- **[measured]**: measured on this machine on 25 Nov 2026 with [2026-11-25-measure-sessions.py](2026-11-25-measure-sessions.py), which reads the session records read-only and prints counts and token figures only. No message text, title, folder name or id is in this report.
- **[reasoned]**: worked out here from the above. Unproven until a trial says otherwise.

Prices use Opus 5.5 list rates, since his sessions are on Opus: base input $4 per million tokens, one-hour cache write $8, cache read $0.20 **[doc 25 Nov]** ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)). He pays a subscription, not per token, so dollars here are a common unit for weighing one choice against another, not his bill **[reasoned]**.

## The short answer

He can have most of both. What he types is small: in his long sessions it is a median of under 1% of the context **[measured]**. Keeping every word he typed, verbatim, costs a few thousand tokens. The rest of a long session (tool output, file reads, the model's own answers) is what makes it expensive, and that is what a fresh start drops.

The expensive moment is not the long session itself. It is coming back to one after the cache has gone cold: those returns are 63% of all cache writes in his sessions and about a third of their input-side cost **[measured]**. The cheap time to prepare a switch is while the cache is still warm, and the room today only speaks after it has gone cold.

The vendors already do half of this. Codex keeps the newest 20,000 tokens of the user's own messages word for word through every compaction **[doc 25 Nov]**. Claude Code's summary "keeps your requests" but warns that "detailed instructions from early in the conversation may be lost", and does not keep them verbatim **[doc 25 Nov]**. The room should copy what Codex does, on both vendors, through documented hooks.

## 1. What vendor compaction keeps and loses

**Claude Code** (all [doc 25 Nov]):

- "Claude Code manages context automatically as you approach the limit. It compacts the conversation into a summary. The summary keeps your requests and key code snippets, but detailed instructions from early in the conversation may be lost. Put persistent rules in CLAUDE.md." ([how Claude Code works](https://code.claude.com/docs/en/how-claude-code-works))
- After compaction these come back from disk: the project CLAUDE.md and unscoped rules, auto memory, skill descriptions, a fresh git status, the plan, up to five recently edited files, invoked skill bodies (5,000 tokens each, 25,000 total), and the output of SessionStart hooks matching `compact`. "Context that hooks added earlier" is "summarized with the rest of the conversation". ([context window, "What survives compaction"](https://code.claude.com/docs/en/context-window))
- His messages are not kept word for word. Nothing in the documentation says they are, and the summary in the docs' own example is 1,725 tokens for a 22,225-token session.
- What can be steered: a "Compact Instructions" section in CLAUDE.md; `/compact <focus>`; a **PreCompact** hook whose stdout "becomes instructions for the compaction summary" (up to 10,000 characters) and which can block compaction; a **PostCompact** hook that receives the summary text as `compact_summary`; a **SessionStart** hook with source `compact` whose output is added after compaction; `/autocompact` to set when it fires; `idleCompaction` to stop it compacting an idle session. ([hooks](https://code.claude.com/docs/en/hooks), [costs](https://code.claude.com/docs/en/costs), [prompt caching](https://code.claude.com/docs/en/prompt-caching))
- Every hook receives `transcript_path`, the session's JSONL record, which "may lag the in-memory conversation". The record's format is not documented as an interface.
- Published quality measure: none found. The docs describe the summary; they do not measure it.

**Codex** ([doc 25 Nov]: the openai/codex source at main `cec14d2`, and the [config reference](https://learn.chatgpt.com/docs/config-file/config-reference)):

- The summary prompt (`codex-rs/prompts/templates/compact/prompt.md`) asks for "a handoff summary for another LLM that will resume the task" with progress and decisions, "Important context, constraints, or user preferences", next steps and critical data, "concise, structured".
- After compaction the history is rebuilt from the summary plus the user's own messages, newest first, kept verbatim until a budget of `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000` runs out (`codex-rs/core/src/compact.rs`). Older user messages beyond the budget are dropped from the verbatim part.
- Steerable: `compact_prompt` or `experimental_compact_prompt_file` to replace the summary prompt, `model_auto_compact_token_limit` for when it fires, and PreCompact and PostCompact hook events. Secondary sources say OpenAI-hosted models compact server-side and ignore `compact_prompt`; that was not confirmed in the docs, so treat it as unproven.
- Published quality measure: none found.

**What this means** **[reasoned]**: on Claude Code, a correction he made early in a long session survives only if the summary chose to keep it. The room can change that with documented surfaces: the PreCompact hook tells the summary to quote him, and the SessionStart `compact` hook re-adds his recent words verbatim, which is what Codex does by itself.

## 2. Carrying on against switching

**What the cache does on his machine** **[measured]** (352 of his own desktop sessions; 764 headless room sessions shown for contrast):

| Gap before he returned | Returns | Rewrote over half the context | Context at return (median) |
|---|---|---|---|
| 5 to 55 minutes | 1,363 | 30 | 365k |
| 55 to 65 minutes | 23 | 7 | 401k |
| over 65 minutes | 383 | 367 | 297k |

The one-hour cache lifetime in the docs holds on this machine. 367 cold returns rewrote 131M tokens: 63% of all cache writes in his sessions, and about 31% of their input-side cost at list rates (writes and reads; output not counted). 278 of the 367 happened above 150k tokens of context. A fresh session's first request is a median of 54k tokens (p10 37k, p90 62k) before he types anything.

**The price of one turn** **[reasoned]** from the rates above:

| Context | Warm turn (read) | Cold turn (rewrite) | Fresh start (54k written) |
|---|---|---|---|
| 150k | $0.03 | $1.20 | $0.43 |
| 400k | $0.08 | $3.20 | $0.43 |
| 800k | $0.16 | $6.40 | $0.43 |

A cold turn costs 40 times a warm one on Opus 5.5. After a cold return the session is warm again, so the extra cost of carrying on is the one rewrite plus about (C - 54k) x $0.20 per million per turn: at 400k that is about $0.07 a turn.

**The cost of a handover.** Writing a brief means one request that reads the whole context. Warm, that is a read ($0.08 at 400k) plus the brief's output. Cold, it is the same full rewrite as carrying on ($3.20 at 400k). So a brief written after the cache has gone cold saves nothing on that return, and a brief written while the cache is warm costs almost nothing **[reasoned]**. The room's handover (`room handoff`) has been used twice and has written no lineage row **[measured]**: the ledger has 2 `handoff` marks and an empty `lineage` table.

**Quality as context grows** **[study]**: [Context Rot](https://research.trychroma.com/context-rot) (Chroma, July 2025) measured 18 models on simple retrieval and repetition tasks and found reliability falls as input grows, even when the task does not get harder. [LLMs Get Lost in Multi-Turn Conversation](https://arxiv.org/abs/2505.06120) (Laban et al., 2025) measured an average 39% drop on six tasks when the same request is spread over turns, mostly as unreliability, and found models that take a wrong turn early rarely recover. [LongMemEval](https://arxiv.org/abs/2410.10813) measured 30 to 60% drops for long-context models answering over long chat histories. None of these measured Opus 5.5 or a 400k coding session, so the size of the effect on his sessions is unknown. What they support is that a long session is not free in quality either, and that a fresh session with the right material in front of it can answer better than the long one.

**What a session can weigh** **[reasoned]**: switching wins when the brief is ready, the next work is new rather than a continuation, and the context is several times a fresh start. Carrying on wins when the next turn depends on detail in the context that no brief holds. A session can say this to him in one line with the numbers: "Coming back now rewrites 400k tokens, about 40 normal turns' worth. A brief written an hour ago is ready; a fresh session from it starts at about 60k."

## 3. Keeping his words verbatim at low cost

How much he types **[measured]**: in his 72 sessions that peaked above 150k, the text he typed totals a median of 8,800 characters (about 2,200 tokens), p75 27,700, p90 60,000. As a share of peak context it is a median of 0.7%. Long pastes inflate the top end (one session's typed text exceeds its context, which can only be pasted material), so a record of his words should keep pastes apart or as a pointer.

| Option | Tokens per request | What is lost | How it goes stale | Survives a vendor format change |
|---|---|---|---|---|
| **A. His words file**, carried whole into the fresh session | His typed text: median about 2k tokens, p90 about 15k, read from cache after the first turn | What his words referred to; the agreements the model proposed and he accepted with "yes"; pastes unless kept as pointers | Superseded lines stay unless ordered and dated; a later correction must win | Yes, if captured by the documented `UserPromptSubmit` hook. No, if parsed from the JSONL record |
| **B. Retrieval over the old transcript** when a question needs it | 0 until asked; a few thousand per lookup | Whatever nobody thinks to ask about: a session does not know what it does not know | Never stale: it is the record | No: the JSONL format is not a documented interface. layer.md also says the room builds no transcript search |
| **C. Fresh session reads its predecessor's record on demand** (grep the file it is pointed at) | As B, with no index to maintain | As B, plus the risk of reading a huge file whole | Never stale | Mostly: a text search survives most changes, but the session may misread new fields |
| **D. Summary that quotes him** where it matters (handover brief, or the compaction summary steered by a PreCompact hook) | 1k to 3k | Everything the summariser did not pick. Extraction loses: [Fidelity Before Structure](https://arxiv.org/abs/2601.00821) measured 16 to 22 points less accuracy for extracted facts than for verbatim chunks [study] | Each brief is a snapshot | Yes: briefs are the room's own files; PreCompact is documented |
| **E. Vendor compaction alone** | Summary about 2k plus re-read files and memory | Claude Code: his words are paraphrased, early instructions "may be lost" [doc]. Codex: user messages older than the newest 20k tokens | Refreshed at each compaction, losing a little more each time | Yes, by definition; but what it keeps can change without notice |
| **F. A + D + C together** (recommended) | The newest of his words verbatim up to a budget (Codex uses 20k), plus a quoting summary, plus a pointer to the full words file and transcript | Words older than the budget are not in front of the session, but are one read away and the session is told where | Dated lines, newest first; a correction later in time is read as winning | Yes: hooks and the room's own files only |

Two studies back F over a store of extracted facts **[study]**: [Fidelity Before Structure](https://arxiv.org/abs/2601.00821) (verbatim chunks 67.4% against 45.4% for extracted artifacts on LongMemEval-S, everything else held constant) and [Do Conversational Agents Need Explicit Memory?](https://arxiv.org/abs/2610.24671) (Oct 2026: six memory systems showed "no consistent advantage over strong raw-history RAG baselines", and some lost the answer while building memory). Both measured question answering over chat histories, not coding agents. Codex's own design, the newest user words verbatim plus a summary, is the same idea shipped by a vendor **[doc 25 Nov]**.

## 4. What has already been studied

| Work | What it measured, on what | Bearing here |
|---|---|---|
| [Context Rot](https://research.trychroma.com/context-rot), Chroma, 2025 | 18 models, simple tasks, growing input length | Quality falls with context even on easy tasks |
| [LLMs Get Lost in Multi-Turn Conversation](https://arxiv.org/abs/2505.06120), Laban et al., 2025 | 6 generation tasks, single-turn against sharded multi-turn, many models | 39% average drop; early wrong assumptions persist. A fresh start with a clean statement can beat a long thread |
| [LongMemEval](https://arxiv.org/abs/2410.10813), Wu et al., ICLR 2025 | Chat assistants answering over long multi-session histories | 30 to 60% drops for long-context models; memory design split into indexing, retrieval, reading |
| [Fidelity Before Structure](https://arxiv.org/abs/2601.00821), 2025 to 2026 | One retrieval pipeline, only the stored form changed; LoCoMo and LongMemEval-S | Verbatim beats extracted by 16 to 22 points |
| [Do Conversational Agents Need Explicit Memory?](https://arxiv.org/abs/2610.24671), Oct 2026 | Six memory systems against raw-history retrieval; LongMemEval-S, BEAM-100K | No consistent win for memory systems; some lose the answer when building memory |
| [Evaluating AGENTS.md](https://arxiv.org/abs/2602.11988), Gloaguen et al., ICLR 2026 | Coding agents on SWE-bench and a new benchmark, with and without context files | No improvement in success, over 20% more cost; keep files to minimal requirements |
| [How Many Instructions Can LLMs Follow at Once?](https://arxiv.org/abs/2507.11538), 2025 | 20 models, up to 500 simultaneous instructions | Compliance falls as instructions pile up, and earlier ones are favoured |
| [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657), 2025 | 1,600+ traces across 7 multi-agent frameworks | "Loss of conversation history" and ignoring another agent's input are named failure modes of handoffs |
| [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), Anthropic | *Vendor claim.* Compaction, note-taking outside the context, sub-agents | Recommends the smallest high-signal context; not a measurement |
| Claude Code docs: CLAUDE.md "under 200 lines", auto memory "first 200 lines or 25KB" [doc 25 Nov] | *Vendor claim*, no measurement given | The vendor's own size guidance is several times the room's cap |

## 5. Is the person file the right tool, and is a fixed cap right?

**What belongs where** **[reasoned]**:

- **Loaded in every session (the person file):** who he is, how he wants to be worked with, and his standing principles. A dozen to a few dozen lines. "Sessions keep their agency and judge; no hard rules he has to fight" belongs here.
- **One step away, fetched when needed:** each place's card (#572) for what a project is and who holds it; his words record for a session or place; the transcripts.
- **In a handover:** the state of the work, and his words from that session (option F), not a rewrite of them as rules.

The person file is the right tool for the first list and the wrong one for the other two. His working norms from his long session are of two kinds: standing principles (person file) and facts about places, like which session answers which kind of question (cards and routes, #560 and #572).

**The cap.** The token cost of the file is not the problem: 800 tokens read from cache cost $0.00016 a turn, and 3,000 would cost $0.0006 **[reasoned]**. What the evidence supports is that more instructions lower compliance and raise exploration ([AGENTS.md study](https://arxiv.org/abs/2602.11988), [IFScale](https://arxiv.org/abs/2507.11538)) **[study]**. It supports "small and minimal". It does not support 60 lines or 800 tokens, and neither study gives a cliff at any size. The vendor's own guidance is 200 lines **[doc 25 Nov]**. A refusal at a guessed number is a hard rule of the kind he described fighting. The evidence supports a limit that reports the running size and cost on each addition and says when the file has passed a target, and leaves the judgement to whoever is writing; the steward reports a file that stays over the target. The secret check is different: refusing a secret prevents harm that cannot be undone, and it should stay a refusal, with the false-positive in #599 fixed.

## 6. What carries a principle to every session?

In the case in the issue, the principle sat in one folder's memory and in `docs/owner-statements.md`. A session started elsewhere loads its own folder's CLAUDE.md and memory, the user-level `~/.claude/CLAUDE.md`, and whatever the brief says **[doc 25 Nov]**, so none of those reached it, and the brief carried rules instead.

**Mechanisms that would have carried it** **[reasoned]**:

1. **The person file loaded at session start everywhere** (#575, through the user-level instruction file or the plugin's SessionStart hook, which the docs say run in every folder). Cost: the line itself, about 40 tokens a request, read from cache. This is the one that would have worked without anybody remembering.
2. **The brief quoting him.** The handover template has a "From the session" section. If the brief puts his words in, verbatim, instead of turning them into instructions, the principle arrives in his voice. Cost: a few hundred tokens once.
3. **Not** a memory in one folder, and not `owner-statements.md`, which no session outside this repo reads.

## The room's present numbers

| Number | Evidence | Verdict and replacement |
|---|---|---|
| `cold_return_minutes = 60` | Vendor docs: the main conversation gets a one-hour cache on a subscription. Measured: after 5 to 55 minutes 2% of returns rewrote the context, after 65 minutes 96% did | **Supported.** Keep it; it is the vendor's number, not a guess. Read it from the vendor's setting if `promptCacheTtl` is set |
| `cold_return_context = 150000` | Cost of a cold return is linear in context; there is no natural step at 150k. 278 of 367 cold returns were above it | **Not supported as a fixed number.** Replace with one derived from the place's measured fresh-start size: speak when a cold rewrite costs more than about three fresh starts (about 160k today), and always state the cost in numbers |
| `person_max_lines = 60` | Studies support small; none gives a line count. Vendor guidance is 200 lines | **Not supported as a refusal.** Keep as a target that is reported, not enforced |
| `person_max_tokens = 800` | Money cost is negligible when cached; the cost is attention, which no study ties to 800 | **Not supported as a refusal.** Report size and cost on each addition; steward finding when over target |

Changing any of them is for the issues below, not this report.

## Recommendation

**Keep his words, not a summary of them, and prepare the switch while the cache is warm.**

1. **A words record.** A `UserPromptSubmit` hook appends each message he types, dated, to a per-session file in the room home, with pastes over a few thousand characters kept as a pointer. Documented surface; no transcript parsing.
2. **Carry the newest words through compaction and into fresh sessions.** A PreCompact hook tells the summary to quote him on decisions and corrections; a SessionStart hook (`compact`, and `startup` when taking a handover) adds his newest words verbatim up to a budget, newest first, and the path to the rest. Codex's 20,000 tokens is the starting budget, and the trial below tests it.
3. **Offer the switch before the cache goes cold, not after.** When a session has a large context and he has stopped, a Stop-time step writes the brief while the read is cheap, so the offer on a cold return has a ready brief behind it. The notice gives the cost in numbers and stays an offer.
4. **The person file reports instead of refusing**, and carries his standing principles, starting with "no hard rules he has to fight". Secrets stay refused.

Sessions keep the judgement throughout: nothing here blocks anything.

### Slices to build (draft issues)

1. *Words record and carry-over* (recommendation 1 and 2). Opened as a `kind:suggestion` issue.
2. *Cold-return notice with numbers and a brief prepared while warm* (recommendation 3, the `cold_return_context` replacement). Opened as a `kind:suggestion` issue.
3. *Person file reports size and cost instead of refusing; secrets still refused* (recommendation 4, the two caps). Opened as a `kind:suggestion` issue.
4. *Handover template quotes him* in "From the session", and its instructions say to quote, not restate as rules. Small; can ride with slice 1. Not opened: three per run.
5. *A trial measure for owner sessions.* `room trials` today only splits dispatched tasks, and its only measures are per landing. The trial below needs a measure for owner sessions. Not opened: three per run.

### Trial proposal

`room trials` cannot run this one yet: it assigns arms to dispatched tasks and can vary only `resume_window_seconds`, `rework_resume_seconds` and `run_timeout_seconds`. Written in its format so slice 5 knows what it needs:

```toml
# Does carrying his newest words verbatim cut the corrections he repeats? (commonroom#626)
[[trial]]
name = "words-carry"
area = "owner-sessions"
started = "YYYY-MM-DD"           # when slices 1 and 5 are live
share = 0.5                      # half his fresh or compacted sessions get the words block
measure = "repeated_corrections_per_session"   # new: needs slice 5
min_landings = 10                # here: sessions per arm that pass 150k or take a handover
max_days = 14

[trial.variant]
policy = { words_carry_tokens = 20000 }   # control: 0 (summary only)
```

Pass: the variant's repeated corrections per session are lower and its tokens at session start rise by less than 20k. Fail: no difference in corrections, which would mean the vendor's summary is enough and slice 1's carry-over should be dropped.

## The two tests the build is judged by

1. **His test: a question for another project goes to the right place.** In a fresh session started in his home place, he asks ten questions that belong to other projects, taken from real ones he has asked before and written down by the director with him in advance. Pass: at least nine name the right place or holder, or route there, and he sends no message explaining context he has given before. One explanation is a fail for that question.
2. **His words survive a switch.** From a session that passed 300k tokens, the room switches to a fresh one by handover. He picks five subjects he spoke about in the old session. Pass: for at least four, the fresh session gives his words as he typed them (checked by string match against the words record), and in the week after, he repeats no correction the old session already had (counted by him or by the page's corrections count once layer.md conclusion 8 is built).

## What this report did not verify

- Whether Claude Code's compaction summary keeps any of his sentences verbatim in practice. Docs only; no summary was read, because that would mean reading session content.
- Whether `idleCompaction` already shrinks his cold returns. The figures above show 96% of returns after an hour still rewrote the full context, so on his installed version it is not doing so, but the version was not checked.
- Codex's server-side compaction for OpenAI models: secondary sources only.
- The quality effect of context size on Opus 5.5 specifically. No study measured it.
- His typed-text figures count every message that is not a tool result, command or reminder. Pasted material is included and inflates the top of the range.
