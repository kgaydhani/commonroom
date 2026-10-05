@echo off
rem Common Room heartbeat: keep the project services up, then run the dispatcher until there is nothing to do.
rem The dispatcher lock makes overlapping runs exit at once. Usage: heartbeat.cmd <project dir>
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
cd /d "%~1" || exit /b 2
"%PY%" "%~dp0room.py" services >> "%USERPROFILE%\.commonroom\heartbeat.log" 2>&1
"%PY%" "%~dp0room.py" run --until-idle >> "%USERPROFILE%\.commonroom\heartbeat.log" 2>&1
