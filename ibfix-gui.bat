@echo off
rem L4D2 Index Buffer Fixer - graphical front-end
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -c "from l4d2_ibfix.gui import main; main()" & goto :eof)
python -c "from l4d2_ibfix.gui import main; main()"
