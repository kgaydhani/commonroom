@echo off
rem Common Room heartbeat: keep the project services up, then run one dispatcher per provider until idle.
rem Each dispatcher has its own lock, so overlapping heartbeats exit at once. Usage: heartbeat.cmd <project dir>
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
cd /d "%~1" || exit /b 2
set "LOG=%USERPROFILE%\.commonroom\heartbeat.log"
"%PY%" "%~dp0room.py" services >> "%LOG%" 2>&1
start "" /b cmd /c ""%PY%" "%~dp0room.py" run --until-idle --provider codex >> "%USERPROFILE%\.commonroom\heartbeat-codex.log" 2>&1"
"%PY%" "%~dp0room.py" run --until-idle --provider claude >> "%LOG%" 2>&1
