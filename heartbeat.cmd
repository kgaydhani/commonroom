@echo off
rem Common Room heartbeat: run the dispatcher until there is nothing to do. The lock makes overlapping runs exit at once.
rem Usage: heartbeat.cmd <project dir>
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
cd /d "%~1" || exit /b 2
"%PY%" "%~dp0room.py" run --until-idle >> "%USERPROFILE%\.commonroom\heartbeat.log" 2>&1
