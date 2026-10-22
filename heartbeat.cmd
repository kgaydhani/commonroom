@echo off
rem Common Room heartbeat: keep the project services up, then run one dispatcher per provider until idle.
rem Each dispatcher has its own lock and its own log (Windows blocks two processes appending to one file).
rem Usage: heartbeat.cmd <project dir>
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
cd /d "%~1" || exit /b 2
set "LOGS=%USERPROFILE%\.commonroom"
rem The project this heartbeat runs for is the host: a guest project (one with [share], like Common Room's own) rides
rem on its plan, whichever project that is. commonroom#151.
set "COMMONROOM_HOST=%CD%"
rem In the block below, in order:
rem - a meter probe (no inference): after a usage window resets, the gate holds every provider until a fresh reading
rem   arrives, and with no interactive session nothing else takes one (18 Oct: the room sat idle overnight).
rem - project services, then the project's own [heartbeat] commands (room tick), e.g. a queue runner. Quick.
rem - one Codex dispatcher, and extra Claude slots 2 and 3: they build in parallel only when the queue is deep and
rem   weekly use is well under pace. See [parallel] in room.toml.
rem - Common Room's own dispatcher, run from this checkout: the room improves itself as a guest on the allowance of
rem   the project this heartbeat runs for, with its share capped by [share] in this folder's room.toml. commonroom#31.
rem - the project's Claude dispatcher, in the foreground.
rem The room fast-forwards this checkout after it merges its own PRs, which can rewrite this file while it runs. cmd.exe
rem reads a batch file one line at a time, so everything below is one block: cmd reads the whole block before running
rem any of it, and the block exits before cmd would read on. Keep comments out of the block.
(
  "%PY%" "%~dp0room.py" probe >> "%LOGS%\heartbeat-probe.log" 2>&1
  "%PY%" "%~dp0room.py" services >> "%LOGS%\heartbeat-services.log" 2>&1
  "%PY%" "%~dp0room.py" tick >> "%LOGS%\heartbeat-tick.log" 2>&1
  start "" /b cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider codex >> "%LOGS%\heartbeat-codex.log" 2>&1"
  start "" /b cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider codex --slot 2 >> "%LOGS%\heartbeat-codex2.log" 2>&1"
  start "" /b cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider claude --slot 2 >> "%LOGS%\heartbeat-claude2.log" 2>&1"
  start "" /b cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider claude --slot 3 >> "%LOGS%\heartbeat-claude3.log" 2>&1"
  if exist "%~dp0room.toml" start "" /b /d "%~dp0." cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider claude >> "%LOGS%\heartbeat-commonroom-claude.log" 2>&1"
  "%PY%" "%~dp0room.py" run --until-idle --provider claude >> "%LOGS%\heartbeat-claude.log" 2>&1
  exit /b
)
