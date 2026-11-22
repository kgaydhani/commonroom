# Codex CLI hooks and transcripts: can one room plugin serve both vendors? (#369)

Scouted 2026-11-22 for slice 3 of `docs/design-v3.md`, "Attaching a session". Read-only: no Codex model call was made,
nothing was installed, and no Codex config was changed. Local rollout files were read for their line types, field names
and token numbers only, never message text.

**Short answer.** Yes, with one fix. Codex has both events and takes the same output JSON as Claude Code, so
`commonroom/hook.py` needs no change in what it reads or writes. **The plugin's `hooks.json` would not work under Codex
as written.** Codex's command handler has no `args` field. It would run a bare `python` through `cmd /C`, and that would
fail on the hook JSON. The fail is not blocking, because Codex's default `onFailure` is continue. Codex also needs the
owner to trust each hook once in `/hooks`, and probably runs plugins from a copy, so `../room.py` may not resolve. Codex
writes a rollout transcript with per-call token usage. Its shape differs from Claude Code's, and OpenAI says the format
is not a stable interface.

## Sources

| Ref | Source | Date or version |
|---|---|---|
| D1 | Codex hooks docs, https://learn.chatgpt.com/docs/hooks (`developers.openai.com/codex/hooks` 308-redirects here; markdown at `.../hooks.md`) | Fetched 2026-11-22. The page gives no date. It calls itself the release-behaviour reference, and says the `main` schemas may hold unreleased fields |
| S1 | `codex-rs/protocol/src/hooks.rs`, `HookHandlerConfig` (https://github.com/openai/codex/blob/main/codex-rs/protocol/src/hooks.rs) | Last changed 2026-11-20 (`a1afa12273`) |
| S2 | `codex-rs/hooks/schema/generated/{session-start,user-prompt-submit}.command.{input,output}.schema.json` (https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated) | Last changed 2026-11-15 (`3b8bd2ee7e`, openai/codex#61587) |
| S3 | `codex-rs/hooks/src/engine/command_runner.rs`, `build_command` | `main`, 2026-11-22 |
| S4 | `codex-rs/exec-server-protocol/src/protocol.rs`, `DISCOVERABLE_PLUGIN_MANIFEST_PATHS`; test `plugin_manifest_uses_alternate_discoverable_path` in `codex-rs/core-plugins/src/manifest.rs` | Last changed 2026-11-16 |
| S5 | `codex-rs/history/src/rollout_payload.rs`, `RolloutItemWire::TokenUsageRecord`. Added by "Persist response token usage in rollout history" (openai/codex#41912) | 2026-08-31 |
| L1 | Local Codex on this PC: `%LOCALAPPDATA%/OpenAI/Codex/bin/9e1da79431da526b/codex.exe --version` gives `codex-cli 0.167.0-alpha.4.1`. `codex features list` shows `hooks stable true` and `plugin_hooks removed false` | 2026-11-22. `environment.md` still says 0.160.0. The latest GitHub release is `rust-v0.171.0` (2026-11-20) |
| L2 | The 5 local rollouts in `~/.codex/sessions/2026/11/`, all `cli_version` 0.167.0-alpha.4.1: line types, field names and token numbers only | 2026-11-21 and 2026-11-22 |
| C1 | Claude Code hooks docs, https://code.claude.com/docs/en/hooks (markdown at `.../hooks.md`) | Fetched 2026-11-22 |

## 1. Does Codex have SessionStart and UserPromptSubmit? Where are they registered, what do they receive, how do they add context?

**Yes, both, under the same names** (D1, S1). Codex also has PreToolUse, PermissionRequest, PostToolUse,
PreCompact, PostCompact, SubagentStart, SubagentStop, Stop, Interrupt and SessionEnd, plus the observe-only
PreModelRequest and PostModelRequest.

**Where they are registered** (D1). Codex reads all of these and runs every matching hook. A higher layer does not
replace a lower one, and matching command hooks for one event start concurrently.
- User: `%USERPROFILE%\.codex\hooks.json`, or a `[hooks]` table in `~/.codex/config.toml`.
- Project: `.codex/hooks.json` or `.codex/config.toml`, only in a **trusted** project. Codex walks the `.codex`
  folders from the project root down to the cwd.
- System: `%ProgramData%\OpenAI\Codex\hooks.json`. Managed: `[hooks]` in `requirements.toml`.
- **Plugin:** `hooks/hooks.json` in the plugin root by default, or a `hooks` entry in `.codex-plugin/plugin.json`.
  Codex also finds a plugin whose manifest is at **`.claude-plugin/plugin.json`** (S4: the discoverable paths are
  `.codex-plugin/`, `.claude-plugin/` and `.cursor-plugin/plugin.json`, and a test loads a plugin from the Claude path).
  So our `plugin/` folder is discoverable as it stands.
- **Trust:** every non-managed hook, plugin hooks included, must be reviewed and trusted in `/hooks` (in the desktop
  app, Review hooks or Settings > Hooks) before it runs. Trust is tied to a hash, so a changed hook, or a changed bundled
  script, needs a new review. `--dangerously-bypass-hook-trust` skips this for one run. Trusting is an owner action.

**Input on stdin** (D1, and S2's input schemas, which set `additionalProperties: false`):

| Field | SessionStart | UserPromptSubmit |
|---|---|---|
| `session_id` | yes (a subagent's hooks get the parent's id) | yes |
| `transcript_path` | yes, **string or null** | yes, string or null |
| `cwd` | yes | yes |
| `hook_event_name` | `"SessionStart"` | `"UserPromptSubmit"` |
| `model` | yes (Codex extension) | yes |
| `permission_mode` | yes | yes |
| `source` | `startup`, `resume`, `clear`, `compact` or `fork` | |
| `turn_id` | | yes (Codex extension) |
| `prompt` | | yes |
| `agent_id`, `agent_type` | | optional (in a subagent) |

**Adding context without blocking** (D1). For both events:
- Plain text on stdout is added as developer context.
- One JSON object on stdout, `{"hookSpecificOutput": {"hookEventName": "<event>", "additionalContext": "..."}}`, is
  added as developer context. `hook.py` already writes exactly this.
- Exit 0 with no output means nothing is added.
- Only UserPromptSubmit can block: with `{"decision": "block", "reason": ...}` or with exit code 2. A hook that never
  writes `decision` and always exits 0 cannot block.
- A failing hook does not block either: `onFailure` defaults to continue (S1).
- Context over about 2,500 tokens is saved to disk and the model gets a preview (`additionalContextLimit` changes
  this). Our paragraph is far below it.
- Hooks run with the session's cwd as their working directory. The default timeout is 600 s, so our handler's
  `"timeout": 5` and `hook.py`'s own 2-second guard still apply.

## 2. Can one hook command serve both vendors?

**The program, yes. The current `hooks.json` entry, no.**

`hook.py` reads `hook_event_name`, `session_id` and `cwd`, and those mean the same in both. It writes the
`hookSpecificOutput.additionalContext` shape, and both accept it. So `room hook session-start`, and #367's
`room hook prompt-submit`, can be one command for both vendors.

The registration is where they differ:

- **No `args` in Codex.** A Codex command handler has `command` (one string), `commandWindows`, `timeout`, `async`,
  `statusMessage`, `onFailure` and `additionalContextLimit`, and no `args` (S1). On Windows Codex runs
  `cmd.exe /C "<command>"` (S3). Our entry is `"command": "python", "args": [...]`, which is Claude Code's exec form
  (C1). Under Codex that would run a bare `python` with the hook JSON on stdin. Python would try to run the JSON as a
  script and fail, so the hook would add nothing and Codex would mark it failed. This is read from the source, not
  tested. S1 has no `deny_unknown_fields`, so `args` is ignored rather than rejected.
- **A possible fix, untested on either vendor:** keep `command` and `args` for Claude Code, and add
  `"commandWindows": "python \"%PLUGIN_ROOT%\\..\\room.py\" hook session-start"` for Codex. Codex sets `PLUGIN_ROOT`
  (and `CLAUDE_PLUGIN_ROOT`) as environment variables. Under `cmd /C` they must be written `%VAR%`, because cmd does not
  expand `${VAR}`. Claude Code would need to ignore `commandWindows`, and its docs don't say whether it does.
- **The plugin may run from a copy.** The bundled Codex plugins on this PC live under
  `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`. If Codex copies a local plugin there too, `PLUGIN_ROOT/..`
  is not the room checkout and the hook can't find `room.py`. I found no source saying whether a local plugin runs in
  place. If it is copied, the hook needs another way to find `room.py`: an absolute path written at install, or a
  `room` command on PATH.

Fields that differ, Codex compared with Claude Code (C1 for Claude Code):

| Field or behaviour | Claude Code | Codex |
|---|---|---|
| `transcript_path` | `~/.claude/projects/<folder>/<id>.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, **or null** |
| `model` | not in these events | always present |
| `turn_id` | none | on UserPromptSubmit |
| SessionStart `source` | `startup`, `resume`, `clear`, `compact` | the same, plus `fork` |
| `permission_mode` values | includes `auto` | `default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions` |
| Subagent hooks' `session_id` | not checked | the parent's id |
| Handler | `command` plus optional `args` (exec form), `shell` | `command` string only, `commandWindows`, `onFailure`, `async` |
| Plugin root variable | `${CLAUDE_PLUGIN_ROOT}`, a placeholder substituted into `command` and `args` | `PLUGIN_ROOT`; `CLAUDE_PLUGIN_ROOT` is set too |
| Trust | none needed for an enabled plugin | must be trusted in `/hooks`, again after each change |
| Context size limit | 10,000 characters | about 2,500 tokens, then saved to disk with a preview |
| Default timeout | 600 s for a command hook, 30 s on UserPromptSubmit | 600 s (1 s for SessionEnd and Interrupt) |

`hook.py` doesn't need to know which vendor called it. If it ever does, `model` or `turn_id` in the input, or
`PLUGIN_ROOT` in the environment, tells them apart. The handshake text says `room attach` and other commands, which are
vendor-neutral.

## 3. Does Codex write a session transcript with per-turn token usage that `sessions.py`'s approach could read?

**Yes, but it needs its own parser**, and OpenAI says the format may change (D1: "the transcript format isn't a stable
interface for hooks and may change over time").

**Path.** `~/.codex/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDTHH-MM-SS>-<thread uuid>.jsonl` (L2). This is the same file
as the hook's `transcript_path` (D1's example uses `.../rollout.jsonl`). Codex also keeps `session_index.jsonl` and
several SQLite stores (`state_5.sqlite`, `thread_history_1.sqlite`). This report did not look inside them.

**Line format** (L2, 0.167.0-alpha.4.1). Each line is JSON with `timestamp` (ISO, `Z`), `ordinal`, `type` and
`payload`. The lines that matter:

```
{"timestamp": ..., "type": "session_meta", "payload": {"session_id", "id", "cwd", "cli_version", "source", "model_provider", "git": {...}, ...}}
{"timestamp": ..., "type": "turn_context", "payload": {"turn_id", "cwd", "model", "effort", ...}}
{"timestamp": ..., "type": "event_msg", "payload": {"type": "token_count",
    "info": {"total_token_usage": {...}, "last_token_usage": {...}, "model_context_window": 475000},
    "rate_limits": {"primary": {"used_percent", "window_minutes", "resets_at"}, "secondary": {...}, "plan_type", ...}}}
{"timestamp": ..., "type": "token_usage_record", "payload": {"thread_id", "turn_id", "session_id", "response_id",
    "usage": {...}, "turn_token_usage": {...}, "thread_token_usage": {...}}}
```

Every usage object has the same six keys: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`,
`output_tokens`, `reasoning_output_tokens` and `total_tokens`.

How this differs from what `sessions._parse` expects:
- **No `cwd` or `sessionId` on the usage lines.** The cwd is in `session_meta`, and again in each `turn_context`. A
  reader has to carry it forward from the latest of those lines, not take it from each line as it does for Claude Code.
  `token_usage_record` has `session_id`; `token_count` has no id at all.
- **One record per model response.** In the local files there are 235 `token_count` lines and 235 `token_usage_record`
  lines, one pair per response, and none had a null `info`. `response_id` is the de-duplication key, where Claude Code's
  is `message.id`.
- **Input includes cached input.** In every local record `cached_input_tokens <= input_tokens`, and
  `total_tokens = input_tokens + output_tokens`. For example, 19,313 input, 12,544 of it cached, 241 output, 19,554
  total. So a call's context is `input_tokens` alone. Summing it with `cached_input_tokens`, as `CONTEXT_KEYS` does for
  Claude Code, would count the cache twice. Reasoning tokens are part of `output_tokens`.
- **Bonus: `token_count.rate_limits`** carries the plan's 5-hour and weekly `used_percent` and `resets_at` at every
  call. That is a usage meter read straight from the transcript, the kind of source design-v3 means by "from Codex's
  app-server". It is unproven as a meter: I haven't checked that it matches what the Codex UI shows.
- **Read the tail.** For #367's "time and context of the last assistant turn", the last `token_count` line gives
  `last_token_usage.input_tokens` and its `timestamp`. A tail read works the same as for Claude Code.

## 4. Is anything experimental or likely to change?

- **Hooks are stable.** D1 marks no hook feature as experimental. `codex features list` on 0.167.0-alpha.4.1 shows
  `hooks stable true` and `plugin_hooks removed`, which means plugin hooks no longer sit behind their own flag (L1).
  They can be turned off with `[features] hooks = false`. `codex_hooks` is a deprecated alias for that flag.
- **The transcript is explicitly not a stable interface** (D1). `token_usage_record` arrived on 2026-08-31 (S5). Any
  reader must skip lines it doesn't understand, as `sessions._parse` already does, and a fixture test should pin the
  format to a version.
- **The hook schema is moving fast.** The model-request hooks were added between 2026-10-18 and 2026-11-15 (S2), and
  `hooks.rs` changed again on 2026-11-20 (S1). D1 warns that `main` may hold unreleased fields. The fields we use
  (`session_id`, `cwd`, `hook_event_name`, `transcript_path`, `additionalContext`) are the Claude-compatible core and
  look the least likely to change.
- **Parsed but not implemented:** `suppressOutput`. Handlers of type `prompt` and `agent` are parsed but skipped (D1).
- **The local version is out of date in our docs.** This PC runs `0.167.0-alpha.4.1` (the desktop app's binary), not
  the 0.160.0 in `environment.md`. The latest release is 0.171.0.

## What this would change

The room can serve Codex sessions with the hook it already has, after a registration fix and one trust step by the owner.
That covers attach, digest pointers, unread notes and #367's cold-return warning, and it costs no model calls. The room
can also measure Codex sessions' context from their rollouts, and possibly read Codex's plan usage without the
hidden-terminal reader.

**Cost:** builder time only. No download, account or payment. Two owner actions: enabling the plugin in Codex and
trusting its hook in `/hooks`. The experiment below spends a few Codex calls on the ChatGPT Plus allowance. Codex has no
reserve.

## Proposed experiment, with a pass/fail test

Done in an interactive session with the owner, because trusting a hook is his action:
1. Add `commandWindows` to the SessionStart entry in a scratch copy of `plugin/hooks/hooks.json`, as in section 2.
2. Add the plugin to Codex from that local path, trust it in `/hooks`, and start `codex` in a registered project's
   folder.
3. Ask the session to repeat any context it got from a hook.

**Pass:** the session repeats the handshake paragraph, the same text `room hook session-start` prints for that cwd.
Claude Code, started with `--plugin-dir` on the same scratch plugin, still repeats it too. **Fail:** either vendor gets
nothing, or `/hooks` shows the hook failed. Then record the failure: whether `PLUGIN_ROOT` points at a cache copy, or
whether Claude Code rejects `commandWindows`.
