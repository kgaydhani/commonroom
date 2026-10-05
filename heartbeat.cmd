@echo off
rem Common Room heartbeat: run the dispatcher until there is nothing to do. The lock makes overlapping runs exit at once.
rem Usage: heartbeat.cmd <project dir>
cd /d "%~1" || exit /b 2
python "%~dp0room.py" run --until-idle >> "%USERPROFILE%\.commonroom\heartbeat.log" 2>&1
