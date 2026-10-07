@echo off
rem Common Room heartbeat: keep the project services up, then run one dispatcher per provider until idle.
rem Each dispatcher has its own lock and its own log (Windows blocks two processes appending to one file).
rem Usage: heartbeat.cmd <project dir>
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
cd /d "%~1" || exit /b 2
set "LOGS=%USERPROFILE%\.commonroom"
"%PY%" "%~dp0room.py" services >> "%LOGS%\heartbeat-services.log" 2>&1
rem Moonshot queue (interim until #24): relaunch dead or capped moonshots, start queued ones. Quick; returns at once.
if exist "D:\Dev\moonshots\msqueue.py" "%PY%" "D:\Dev\moonshots\msqueue.py" tick >> "%LOGS%\moonshot-queue.log" 2>&1
start "" /b cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider codex >> "%LOGS%\heartbeat-codex.log" 2>&1"
rem Extra Claude slots build in parallel only when the queue is deep and weekly use is well under pace ([parallel]).
start "" /b cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider claude --slot 2 >> "%LOGS%\heartbeat-claude2.log" 2>&1"
start "" /b cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider claude --slot 3 >> "%LOGS%\heartbeat-claude3.log" 2>&1"
"%PY%" "%~dp0room.py" run --until-idle --provider claude >> "%LOGS%\heartbeat-claude.log" 2>&1
