@echo off
rem aoiro - register this app with Claude Desktop (Windows)
cd /d "%~dp0"
set PYTHONUTF8=1
set PY=
py -3 --version > nul 2>&1 && set PY=py -3
if not defined PY (
  python --version > nul 2>&1 && set PY=python
)
if not defined PY (
  echo Python was not found. Please install Python first.
  pause
  exit /b 1
)
%PY% -m aoiro mcp-install
pause
