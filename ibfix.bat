@echo off
rem L4D2 Index Buffer Fixer - command line.  Example:  ibfix scan
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)
if "%~1"=="" (
  rem Double-clicked: show help instead of flashing a window shut.
  %PY% ibfix.py --help
  echo.
  echo This is a command-line tool. Open a terminal in this folder and run e.g.:
  echo     ibfix locate
  echo     ibfix scan
  echo Or double-click ibfix-gui.bat for the window version.
  echo.
  pause
  exit /b 0
)
%PY% ibfix.py %*
