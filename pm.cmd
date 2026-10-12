@echo off
rem project-memory CLI launcher for Windows: pm.cmd [--config CONFIG] <command> ...
rem Tries "py -3" first, then "python" from PATH.
setlocal
set "PM_HOME=%~dp0"
set "PYTHONPATH=%PM_HOME%;%PYTHONPATH%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m project_memory %*
  exit /b %ERRORLEVEL%
)
python -m project_memory %*
exit /b %ERRORLEVEL%
