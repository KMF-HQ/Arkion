@echo off
REM ============================================================================
REM  serve/cli.bat  --  ASCII-only executable copy.
REM
REM  Why ASCII only: cmd parses < > | & (and quotes) even inside REM lines, and on a
REM  fresh CP936 console the UTF-8 Chinese comments were byte-misparsed, injecting stray
REM  operators -- the batch structure broke and the window closed instantly (2026-09-30).
REM
REM  Full Chinese notes (original file, verbatim):
REM      docs/_bat_originals/serve_cli.bat.txt
REM  Keep this file ASCII-only when editing. Do not paste Chinese text into it.
REM ============================================================================
chcp 65001 >nul 2>&1
REM ============================================================================
REM
REM ============================================================================
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
python "serve\arkion-cli.py" %*
endlocal
