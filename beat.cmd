@echo off
rem Common Room beat: one beat over every project in the registry (projects.toml). See `room beat`.
rem Usage: beat.cmd   (no project dir: the registry names the projects)
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
cd /d "%~dp0." || exit /b 2
rem A failed beat runs rollback.py: if the room just moved this checkout to new code, it goes back to the last good sha
rem and the room holds the new one (commonroom#453). The room moves this checkout under a running beat, so everything
rem below is one block that cmd reads whole before running it (see heartbeat.cmd). Keep comments out of the block.
(
  "%PY%" "%~dp0room.py" beat >> "%USERPROFILE%\.commonroom\beat.log" 2>&1 || ("%PY%" "%~dp0rollback.py" 1 >> "%USERPROFILE%\.commonroom\beat.log" 2>&1 & exit /b 1)
  exit /b
)
