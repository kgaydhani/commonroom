@echo off
rem Common Room beat: one beat over every project in the registry (projects.toml). See `room beat`.
rem Usage: beat.cmd   (no project dir: the registry names the projects)
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
cd /d "%~dp0." || exit /b 2
(
  "%PY%" "%~dp0room.py" beat >> "%USERPROFILE%\.commonroom\beat.log" 2>&1
  exit /b
)
