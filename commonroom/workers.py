"""Launch one headless worker (Claude Code or Codex) in a directory, stream its output, record usage."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from commonroom import usage
from commonroom.ledger import Ledger


@dataclass
class Role:
    name: str
    model: str | None
    tools: list[str]
    prompt: str


@dataclass
class WorkerResult:
    ok: bool
    session_id: str | None = None
    final_text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    errors: list[str] = field(default_factory=list)
    log: str | None = None


def load_role(project_root: str | Path, name: str) -> Role:
    """Read `.claude/agents/<name>.md`: simple `key: value` frontmatter plus the prompt body."""
    text = (Path(project_root) / ".claude" / "agents" / f"{name}.md").read_text(encoding="utf8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text.replace("\r\n", "\n"), re.S)
    if not match:
        raise ValueError(f"Role file for {name} has no frontmatter")
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    tools = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
    return Role(name, meta.get("model"), tools, match.group(2).strip())


def claude_command(role: Role, model: str | None, session_id: str, prompt_file: Path, effort: str | None) -> list[str]:
    cmd = [usage.claude_executable() or "claude", "-p", "--output-format", "stream-json", "--verbose",
           "--session-id", session_id, "--name", f"room {role.name}",
           "--append-system-prompt-file", str(prompt_file),
           "--permission-mode", "auto", "--permission-prompts", "none", "--no-chrome"]
    if model or role.model:
        cmd += ["--model", model or role.model]
    if effort:
        cmd += ["--effort", effort]
    if role.tools:
        cmd += ["--tools", ",".join(role.tools)]
    return cmd


def codex_command(model: str | None, effort: str | None, cwd: Path, writable: bool) -> list[str]:
    cmd = [usage.codex_executable() or "codex", "exec", "--json", "-C", str(cwd),
           "--sandbox", "workspace-write" if writable else "read-only",
           "-c", 'approval_policy="never"', "-c", "sandbox_workspace_write.network_access=true"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    return cmd + ["-"]


def run_worker(provider: str, role: Role, prompt: str, cwd: Path, ledger: Ledger, log_dir: Path,
               model: str | None = None, effort: str | None = None, timeout: float = 3600,
               stop_check=None) -> WorkerResult:
    """Run to completion. `stop_check()` returning a reason string aborts the worker (e.g. the gate closed)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    log_path = log_dir / f"{int(time.time())}-{provider}-{role.name}.jsonl"
    result = WorkerResult(ok=False, session_id=session_id, log=str(log_path))
    if provider == "claude":
        prompt_file = log_dir / f"{session_id}-role.md"
        prompt_file.write_text(role.prompt, encoding="utf8")
        cmd = claude_command(role, model, session_id, prompt_file, effort)
        stdin_text = prompt
    else:
        # Every worker runs in its own disposable worktree, so Codex may write there (tests, caches, git).
        cmd = codex_command(model, effort, cwd, writable=True)
        stdin_text = role.prompt + "\n\n" + prompt
    env = usage.clean_env()
    env["PYTHONUTF8"] = "1"
    process = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines: queue.Queue = queue.Queue()

    def pump(stream, channel):
        with stream:
            for line in stream:
                lines.put((channel, line))
        lines.put((channel, None))

    for stream, channel in ((process.stdout, "out"), (process.stderr, "err")):
        threading.Thread(target=pump, args=(stream, channel), daemon=True).start()
    process.stdin.write(stdin_text)
    process.stdin.close()
    deadline, ended, last_text = time.monotonic() + timeout, set(), ""
    with log_path.open("w", encoding="utf-8") as log:
        while len(ended) < 2:
            reason = stop_check() if stop_check else None
            if reason or time.monotonic() > deadline:
                result.errors.append(reason or f"timed out after {timeout:.0f}s")
                _kill(process)
                break
            try:
                channel, line = lines.get(timeout=.5)
            except queue.Empty:
                continue
            if line is None:
                ended.add(channel)
                continue
            log.write(json.dumps({"t": round(time.time(), 2), "channel": channel, "line": line}) + "\n")
            log.flush()
            if channel == "err":
                if line.strip():
                    result.errors.append(line.strip()[-500:])
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if provider == "claude":
                observations = usage.from_claude_event(event)
                if observations:
                    ledger.record_usage(observations)
                if usage.claude_overage(event):
                    result.errors.append("Claude reported extra-usage billing; worker stopped")
                    _kill(process)
                    break
                if event.get("type") == "assistant":
                    for block in (event.get("message") or {}).get("content") or []:
                        if block.get("type") == "text":
                            last_text = block["text"]
                if event.get("type") == "result":
                    u = event.get("usage") or {}
                    result.input_tokens = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                    result.output_tokens = u.get("output_tokens")
                    result.cost_usd = event.get("total_cost_usd")
                    result.final_text = event.get("result") or last_text
                    result.ok = not event.get("is_error")
                    if event.get("is_error"):
                        result.errors.append(str(event.get("result"))[-800:])
            else:
                kind = event.get("type")
                if kind == "thread.started":
                    result.session_id = event.get("thread_id")
                elif kind == "turn.completed":
                    u = event.get("usage") or {}
                    result.input_tokens, result.output_tokens = u.get("input_tokens"), u.get("output_tokens")
                    result.ok = True
                elif kind in ("error", "turn.failed"):
                    result.errors.append(json.dumps(event)[-800:])
                item = event.get("item") or {}
                if kind == "item.completed" and item.get("type") == "agent_message":
                    result.final_text = item.get("text", "")
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _kill(process)
    if process.returncode not in (0, None):
        result.ok = False
    return result


def _kill(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        process.kill()
