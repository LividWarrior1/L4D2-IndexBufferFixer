@echo off
rem L4D2 Index Buffer Fixer - graphical front-end
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
%PY% -c "from l4d2_ibfix.gui import main; main()"
if errorlevel 1 pause
