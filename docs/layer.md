# The layer: one install, present in every session

Written on 24 Nov 2026 by the owner's interactive session. It follows three statements he made that day ([owner-statements.md](owner-statements.md), "The layer") and four pieces of research done the same day. He asked for a synthesis that could differ from his lines, and it does in several places; each is marked. It extends [design-v3.md](design-v3.md). Where the two differ, this is newer.

## What he described

One thin layer between him and the AI tools, on a workstation that is "claude first and human second". He talks about anything and it reaches the right project and session. Sessions and context are managed for him. What is known about him and his work follows him into every session. He does not manage folders, sessions or who knows what. It is one installed package, and it never fights what the vendors do well.

## What the evidence says

**His own machine** (a read-only survey of session records: counts, timestamps and file paths, no message text):

- 313 sessions in 41 folders. One project holds 243 of them. About half the folders were used once or twice, and five no longer exist on disk.
- Only 8% of sessions touched a folder outside their own. Crossover feels constant to him and is rare in the record. The count misses reads that leave no path, so the true figure is higher, but it is not most sessions.
- 27 sessions lived more than three days and 23 reached 500,000 tokens of context. Two of them roamed across 17 and 35 other folders: unrelated work sharing one loaded context because that session "knows stuff".
- "Who the owner is" and how he wants to be written for have been learned separately in at least six folders, with differing details. Memory is per folder. His user-level instruction file is seven lines.
- No user-level hooks, plugins or agents are installed.

**What the vendors already provide** (Claude Code docs, read 24 Nov 2026):

- Hooks in the user's settings, or in a plugin installed at user scope, run in every session in every folder, in the terminal and the desktop app. A session-start or prompt hook may add up to 10,000 characters of context. A prompt hook cannot rewrite or redirect the prompt.
- `~/.claude/CLAUDE.md` and `~/.claude/rules/` load in every session, and may import other files. Nothing first-party carries memory across folders beyond these.
- Deny rules block in every permission mode, bypass included. A tool call a hook blocks stays blocked.
- Sessions can message each other on one machine (`SendMessage`, `ListAgents`). A session in another folder can be resumed by id. The session picker searches every project.
- Projects (beta) has a coordinator that routes messages to threads, mostly cloud threads on GitHub repos. Local sessions stay outside it.
- Nothing first-party starts a fresh session with a summary, and the main conversation's cache lasts one hour on a subscription.
- Codex has near-identical hooks and reads `AGENTS.md` (scouted in #369).

**What others built, and what happened** (web research, sources in #554):

- Session and worktree managers that wrap the vendor tools keep being abandoned as the vendors change and ship the same thing: three were deprecated or closed in 2026.
- Memory layers that inject into every prompt cost about 5,000 tokens a request in one independent test, and re-typing the rule worked as well. One long-running personal system found a third of its memories stale after about 30 sessions. Users of both vendors report memory leaking between unrelated projects.
- A 2026 study of agent instruction files found they tended to lower task success and raised cost by more than 20%; only short, developer-written ones helped slightly. Its advice: state the minimum.
- No evidence was found that a cheap model can route a person's requests among dozens of overlapping projects. Router studies describe wrong routes that look like success.
- Nobody ships: one place to talk that reaches the right local project; knowledge of the person that spans projects, stays small and expires; a usage budget across vendors; a decision about when to start fresh.

**This repo on main** (read from the code):

- The hooks fire only inside this repository. A folder that is not a registered project gets nothing. The registry holds two projects and requires a `room.toml` in each.
- Nothing holds knowledge about the owner. Nothing routes a prompt to a project: the working folder decides.
- The guard on dangerous commands covers Claude workers only.
- Starting the room needs a scheduled task made by hand and paths written for one machine.
- Already built and reusable: notes between projects, handing work to another agent, handover briefs with lineage, the cold-return and second-compaction offers, booking his own sessions to projects, a local board for projects with no repository, the steward, one page.

**One afternoon of real use** (a session prepared by hand for the owner on 24 Nov 2026, from one message that spanned his whole life; it sent its findings back through `room note`):

- A prepared start worked. The session read his notes, calendar and each project's record before he arrived, and his first replies were corrections to facts already found. Neither side asked a set-up question.
- Every real deadline came from joining sources: his list read against his email, his calendar and his notes. No single store held any of them.
- Half his record is somewhere the layer cannot see. He uses another vendor's chat app on his phone for quick admin, and none of it had reached his notes or any session. A pasted share link was enough for a session to read it.
- Notes with no reader are dead letters. His notes folder was set up to file its inbox whenever a session opened in it. None did for four weeks, so 24 notes dropped there by other sessions sat unfiled, and the map every session is told to read first described a month earlier. Each writer did its part and the whole failed silently.
- His forward calendar was empty. Every dated fact lived in prose. Writing dates to the calendar he already gets alerts from was the change he noticed first.
- The research pulled the conversation toward what can be looked up. The brief put the time on who he wants to be; an hour in, that question was still open.
- The same was true of the room: a note in a project's inbox wakes nobody. It is seen when a director next runs for another reason.

## Conclusions

**1. The package is the product.** Today the room is a checkout that one person runs by hand. A layer is installed once and is then present everywhere. Packaging was the last slice of design-v3; it moves to first.

**2. The first thing the layer carries is the person, and it must be small.** The measurable waste on his machine is his own profile learned six times and long sessions kept alive for what they know. One short file about him, loaded by the vendor's own mechanism, removes the reason to keep them. The evidence on instruction files and memory layers sets the terms: a hard cap, dated lines, stale lines flagged in code, and nothing injected per prompt. *Differs from his line:* he imagined the room knowing everything on the machine. It should know very little, and know where the rest is.

**3. A map of his folders, never a reorganisation.** Every folder he has worked in becomes a place the room knows: path, a line on what it is, when it was last used, whether it still exists. It is found from the session records the room already reads, with no registration step. Most places never run workers. *Differs from his line:* he asked whether Claude should decide how his folders are organised. A folder per piece of work suits the tools as they are, and moving files breaks sessions, memory and links. The map does the job of the tidy-up.

**4. The session he is in decides where something belongs. The place that owns it does the work.** No classifier sits in front. The session gets the directory at the start: the few active places, what each is for, and who answers for it. When the conversation turns to something another place owns, the session sends it there and stays on its own purpose. It tells him in one line who has it and when to expect an answer, and offers a direct session if it looks like a long exchange. The answer comes back as a short message, and only the answer enters his conversation. Most of the time nothing needs sending. Each route is written to the ledger and shown on the page, so a wrong one is seen.

His words: "the whole point of commonr oom is linking and decluterrign session contexts so that we have extremely able memory separation and we reduce hallucinations". The first draft of this conclusion had the session he was in doing the work with other sessions as sources. Two sessions did exactly that on 24 Nov: one meant for thinking through his life queried another project's database for a purchase, and the session that designs this layer spent an hour debugging another project's nightly job. Both were told to look things up without being told whose things they were.

*Where the first draft differed from his line, and was wrong.* He named his long-lived sessions as the ones with "the full context". The draft said a place should answer from its record, read by a fresh session, because waking a session idle for days rewrites all of its context. The same afternoon a session without that context made a shortlist for him on its own. His verdict: it "decided it knows better than the hours of session turns ive had figuring out how to make a good shortlist [...] and it expects me to trust it that it will be good. this is like the complete opposite of common room". The method had been worked out over hours in one chat and was written down nowhere a fresh session could read.

So the directory points at whoever really holds the knowledge, and today that is often a long-lived session. The cost of waking it is real and is the smaller cost. What the layer adds is the other half: when a place's know-how lives only in a session, that is a gap the place's own session is asked to close, by writing the method into the place's record in its own words. A fresh session answers for a place only once that record can carry it, and he is the judge of when it can.

**5. One home session, kept light, in place of one chat.** A place that is no project: the folder he opens to talk about anything. It holds the map and the person file and no project work, so it stays small and can be replaced at any time. *Differs from his line:* one long chat is the most expensive pattern in his own usage and the one that loses most. If the vendor's coordinator grows to cover local folders, the home session is dropped and the map stays.

**6. Sessions end on purpose.** The cold-return and compaction offers are built. They start working everywhere once the hooks do. One more notice is worth having: a session that has lived for days or wandered across several places is told so once, with a brief ready. It is always an offer.

**7. A few hard limits, in the vendor's own mechanism.** With permissions bypassed, instructions stop nothing. A short, readable list of what must never happen is installed as deny rules, which hold in every mode, plus the existing command guard extended from workers to his own sessions and to Codex. The list is his and he can edit it. This is the one place the layer blocks anything, and the test for axiom A0 changes to say so.

**8. He can only sit back if he can see.** The page gains what the layer did today: what it routed and where, which sessions were offered a fresh start, the size and age of the person file, the limits in force, and how often he had to correct or repeat himself. That last count is the room's measure of whether it is improving the human part.

**9. Nothing is written where nobody reads.** The person file, the map and every inbox need a reader that runs without him, in code where that is possible, and a visible sign on the page when it has not run. A note that has waited too long is a finding like any other the steward raises. This applies to the room's own inboxes first. *This is new:* the design above would have repeated the failure of his notes folder.

**10. Every session feeds the room, without being asked.** When a session anywhere notices something the layer got wrong, could not see or made harder, it sends that to the room as a finding and carries on. It does not stop at telling him. His words: "instead of just telling me this is a gap in common room, i want it to automatically feed that back to u or whoever can do something with that". The session-start line says so in every place, a finding is one command, and a finding reaches the director the way an untriaged issue does. The count of findings, and how many led to a change, goes on the page.

**11. What happened elsewhere comes in through one door.** The layer will never see inside another vendor's phone app, and should not try. It needs one cheap way for him to hand it something from anywhere (a link, a paragraph, a photo of a note) that lands as a note in the right place and is read. Dates found on the way go to the calendar he already uses.

## What the room does not build

Each of these exists first-party or in a maintained tool, and wrappers around them have a record of dying:

- A chat window or a mirror of one.
- A memory store, embeddings, or anything that adds text to every prompt.
- A session or worktree manager.
- Search over old transcripts.
- A model that classifies prompts.
- Anything that moves or renames his files.

## What the layer asks of an AI tool

Four things, and no more: start a session in a folder with a brief; accept added context at session start and on a prompt; report usage; show what landed. Claude Code and Codex meet all four. A tool that meets only the first can be a worker and cannot be talked through.

## Build order

1. **Install.** One command installs the plugin at user scope for Claude Code and the hooks for Codex, registers the beat, and removes machine-specific paths. `room doctor` checks all of it. Removing it is one command.
2. **Places and the directory.** The registry accepts a folder with no `room.toml` and no share. Places are found from session records. Each place says what it is for and who answers for it. The hooks answer in any place.
3. **The person file.** One capped file, loaded through the user-level instruction file of each vendor, with a command to see and edit it and a steward check for stale lines. Filling it from the scattered profiles is done once, with him.
4. **Routes.** The directory at session start; sending a question to the place that owns it and getting a short answer back; a route recorded in the ledger each time; the offer of a direct session; and the home place.
5. **Limits.** The list, its installation as deny rules, and the guard for his own sessions and for Codex.
6. **The glance.** The page's layer section and the corrections count.
7. **Readers and findings.** A steward check for notes nobody has read, the finding route from any session, and the door for what happened elsewhere.

Sharing with a second person (design-v3 slice 6) follows, on the same install.

## How to tell it worked

- A new session in a folder the room has never seen knows who he is, and the layer added under 800 tokens to it.
- Nothing on a second machine is set up by hand.
- A session keeps to its purpose: questions that belong to another place are sent there, and the share of a session's work done in other places' folders falls.
- Over two weeks of his use, the count of corrections and repeats falls, and no session lives past three days without having been offered a fresh one.
- The share of his usage that is carrying context falls from the 90% measured on 20 Nov.
- He has not edited a settings file to make any of it work.

## Risks

- **Wrong routes nobody notices.** Met by showing every route and by never routing silently.
- **The person file goes stale or grows.** Met by the cap and the dated lines. If it cannot be kept under the cap, that is the signal to cut it.
- **The vendor changes underneath.** Met by using only documented surfaces: plugins, hooks, user instruction files, deny rules. No wrapped process, no private endpoint, no parsing beyond the session records already read for booking.
- **A capable session does specialist work badly and sounds sure of it.** Without the method a place has worked out, a session will still produce something plausible. Met by sending the question to the place that owns it, and by the session saying plainly what it does not know.
- **Research crowds out thinking.** A session nudged to look things up will steer toward what can be looked up. The brief template says what the conversation is for, and the session is told to notice when it has drifted from it.
- **With permissions off, a fault in the layer reaches everything.** The hooks stay silent on any error, as they do now, and only the limits can block.
