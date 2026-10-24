"""Launch one headless worker (Claude Code or Codex) in a directory, stream its output, record usage."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from commonroom import usage
from commonroom.ledger import Ledger

RUN_TIMEOUT = 3600   # seconds the room lets one run go; the headless note tells the worker the same number


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
    interrupted: str | None = None      # the room stopped it (gate closed, breaker, timeout)
    limited_until: float | None = None  # the provider refused: usage limit hit, retry after this time


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


ROOM_PY = Path(__file__).resolve().parent.parent / "room.py"


def guard_settings() -> str:
    """`--settings` JSON adding the command guard hook (commonroom#18). Claude Code loads it on top of the user's
    and the project's own settings, and hooks from every settings source run, so the project's hooks still apply."""
    command = f'"{Path(sys.executable).as_posix()}" "{ROOM_PY.as_posix()}" guard-command'
    hook = {"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": command}]}
    return json.dumps({"hooks": {"PreToolUse": [hook]}})


def claude_command(role: Role, model: str | None, session_id: str, prompt_file: Path, effort: str | None,
                   resume: bool = False) -> list[str]:
    # `resume` continues an earlier session with its full context (e.g. one stopped by a budget limit).
    cmd = [usage.claude_executable() or "claude", "-p", "--output-format", "stream-json", "--verbose",
           *(["--resume", session_id] if resume else ["--session-id", session_id]), "--name", f"room {role.name}",
           "--append-system-prompt-file", str(prompt_file),
           "--permission-mode", "auto", "--permission-prompts", "none", "--no-chrome", "--settings", guard_settings()]
    if model or role.model:
        cmd += ["--model", model or role.model]
    if effort:
        cmd += ["--effort", effort]
    if role.tools:
        cmd += ["--tools", ",".join(role.tools)]
    if role.name != "art-tech":
        # Workers don't need the owner's connectors (Drive, Docs, Blender...); they only cost context.
        cmd += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
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


def scratch_dirs(cwd, names: list[str]) -> dict[str, str]:
    """Make `<cwd>/.scratch/<name lowercased>` for each variable name and return the env entries pointing at them.
    The Codex sandbox writes only inside the worktree, so tools that write under e.g. %APPDATA% (Godot) fail there
    (commonroom#81). `.scratch/` goes in the repo's git exclude so it never shows in `git status`."""
    cwd = Path(cwd)
    env = {}
    for name in names:
        folder = cwd / ".scratch" / str(name).lower()
        folder.mkdir(parents=True, exist_ok=True)
        env[str(name)] = str(folder)
    exclude_scratch(cwd)
    return env


def exclude_scratch(cwd: Path):
    done = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0 or not done.stdout.strip():
        return   # not a git working copy: nothing to keep clean
    exclude = Path(cwd) / done.stdout.strip()   # shared by every worktree of the repo
    text = exclude.read_text(encoding="utf8") if exclude.exists() else ""
    if "/.scratch/" in text.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf8", newline="\n") as f:
        f.write(("\n" if text and not text.endswith("\n") else "") + "/.scratch/\n")


def worker_tmp(cwd) -> Path:
    """Per-worktree scratch space on the same drive as the worktree, outside the repo."""
    cwd = Path(cwd)
    return Path(cwd.anchor) / "commonroom-tmp" / cwd.name


def worker_env(provider: str, cwd, scratch_env: list[str] | None = None) -> dict[str, str]:
    """The environment a worker starts with. Makes the temp folder (and any scratch folders) it points at."""
    cwd = Path(cwd)
    env = usage.clean_env()
    env["PYTHONUTF8"] = "1"
    env["COMMONROOM_WORKER"] = "1"   # `room gate` inside a worker answers GO: its start was already gated
    # Git never searches above the working copy: if it is deleted mid-run, the worker's git fails loudly instead of
    # finding the owner's main checkout one level up and committing there (commonroom#17).
    env["GIT_CEILING_DIRECTORIES"] = str(cwd.resolve().parent)
    if provider == "codex":
        # The Codex sandbox writes only inside the worktree, so its temp folder lives there too: tempfile in
        # `commonroom-tmp` failed with WinError 5 and failed tested reviews (commonroom#158).
        env["TEMP"] = env["TMP"] = env["TMPDIR"] = scratch_dirs(cwd, ["tmp"])["tmp"]
    else:
        # Scratch files (render captures run to gigabytes) go on the worktree's drive, never the system drive's
        # TEMP, which filled up on 2026-10-07.
        tmp = worker_tmp(cwd)
        tmp.mkdir(parents=True, exist_ok=True)
        env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp)
    if provider == "codex" and scratch_env:
        # gh keeps its login under %APPDATA%\GitHub CLI: pin it before APPDATA moves, or every Codex run that needs
        # GitHub ends BLOCKED with "authentication missing" (gamemirage#428, 11 Oct).
        if "APPDATA" in scratch_env and "GH_CONFIG_DIR" not in env and env.get("APPDATA"):
            env["GH_CONFIG_DIR"] = str(Path(env["APPDATA"]) / "GitHub CLI")
        env.update(scratch_dirs(cwd, scratch_env))
    return env


def run_worker(provider: str, role: Role, prompt: str, cwd: Path, ledger: Ledger, log_dir: Path,
               model: str | None = None, effort: str | None = None, timeout: float = RUN_TIMEOUT,
               stop_check=None, resume_session: str | None = None, scratch_env: list[str] | None = None) -> WorkerResult:
    """Run to completion. `stop_check()` returning a reason string aborts the worker (e.g. the gate closed).
    `scratch_env` names variables a Codex worker gets pointed at `<cwd>/.scratch/<name>` (`[codex] scratch_env`)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    session_id = resume_session or str(uuid.uuid4())
    log_path = log_dir / f"{int(time.time())}-{provider}-{role.name}.jsonl"
    result = WorkerResult(ok=False, session_id=session_id, log=str(log_path))
    if provider == "claude":
        prompt_file = log_dir / f"{session_id}-role.md"
        prompt_file.write_text(role.prompt, encoding="utf8")
        cmd = claude_command(role, model, session_id, prompt_file, effort, resume=bool(resume_session))
        stdin_text = prompt
    else:
        # Every worker runs in its own disposable worktree, so Codex may write there (tests, caches, git).
        cmd = codex_command(model, effort, cwd, writable=True)
        stdin_text = role.prompt + "\n\n" + prompt
    env = worker_env(provider, cwd, scratch_env)
    tree = ProcessTree()
    process = tree.start(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    try:
        return _stream(process, tree, provider, stdin_text, ledger, log_path, result, timeout, stop_check)
    finally:
        tree.end()   # every process the run started ends with it (commonroom#50)


def _stream(process, tree, provider, stdin_text, ledger, log_path, result, timeout, stop_check) -> WorkerResult:
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
    deadline, ended, last_text, drained = time.monotonic() + timeout, set(), "", None
    with log_path.open("w", encoding="utf-8") as log:
        while len(ended) < 2:
            reason = stop_check() if stop_check else None
            if reason or time.monotonic() > deadline:
                result.errors.append(reason or f"timed out after {timeout:.0f}s")
                if reason:
                    result.interrupted = reason
                tree.end()
                break
            try:
                channel, line = lines.get(timeout=.5)
            except queue.Empty:
                if process.poll() is not None:
                    # A leftover child that inherited the worker's stdout or stderr would hold the pipe open until
                    # the timeout. Ending the tree closes it; a pipe still open after that grace period is abandoned.
                    tree.end()
                    drained = drained or time.monotonic() + 15
                    if time.monotonic() > drained:
                        break
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
                    tree.end()
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
        tree.end()
    if process.returncode not in (0, None):
        result.ok = False
    if not result.ok:
        result.limited_until = usage.limit_hit(result.errors + [result.final_text or ""], time.time())
    return result


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class ProcessTree:
    """A worker and everything it starts (commonroom#50). On Windows the worker runs in a Job Object with
    KILL_ON_JOB_CLOSE, so its children, and their orphans, can't escape it; elsewhere it leads a new process group.
    `end()` kills whatever is left. Only the worker is put in the job: the dispatcher and the services it starts
    stay out. If the job can't be made, `end()` falls back to `taskkill /T`, which reaches only live descendants."""

    def __init__(self):
        self.process = None
        self.job = None
        self.ended = False

    def start(self, cmd, **popen_kwargs) -> subprocess.Popen:
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["start_new_session"] = True
        self.process = subprocess.Popen(cmd, **popen_kwargs)
        if os.name == "nt":
            self.job = _job_for(self.process)
        return self.process

    def end(self) -> None:
        """Kill the worker's whole tree. Only the first call acts, so a later process can't inherit the kill."""
        if self.process is None or self.ended:
            return
        self.ended = True
        if os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        elif self.job is not None:
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.TerminateJobObject(ctypes.c_void_p(self.job), 1)
            kernel32.CloseHandle(ctypes.c_void_p(self.job))
            self.job = None
        elif self.process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _job_for(process) -> int | None:
    """A kill-on-close Job Object holding `process`, or None. The worker is assigned right after it starts, before
    its runtime has loaded far enough to start anything of its own."""
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (kernel32.SetInformationJobObject(ctypes.c_void_p(job), JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                         ctypes.byref(limits), ctypes.sizeof(limits))
            and kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(int(process._handle)))):
        return job
    kernel32.CloseHandle(ctypes.c_void_p(job))
    return None
