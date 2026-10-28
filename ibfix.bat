@echo off
rem L4D2 Index Buffer Fixer - command line.  Example:  ibfix scan
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (py -3 ibfix.py %* & goto :eof)
python ibfix.py %*
