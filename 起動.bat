@echo off
rem aoiro - double-click this file to start (Windows)
cd /d "%~dp0"
set PYTHONUTF8=1
title aoiro

set PY=
py -3 --version > nul 2>&1 && set PY=py -3
if not defined PY (
  python --version > nul 2>&1 && set PY=python
)
if not defined PY (
  echo Python was not found.
  echo Please install Python from the page that opens.
  echo Check "Add python.exe to PATH" during installation.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

%PY% -m aoiro app
if errorlevel 1 pause
