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
REM [BAT-UX 2026-10-01] With no arguments this used to print argparse's help and exit
REM   immediately -- a double-click looked like a crash.  Explain itself, then hold the
REM   window open.
if "%~1"=="" (
  echo [cli.bat] Arkion-Q1 control CLI -- this one needs a subcommand.
  echo.
  echo [cli.bat]   cli.bat status     show server state
  echo [cli.bat]   cli.bat config     show the runtime configuration
  echo [cli.bat]   cli.bat models     list the available models
  echo [cli.bat]   cli.bat chat       chat in this window
  echo [cli.bat]   cli.bat bench      measure speed
  echo.
  echo [cli.bat] To simply chat, double-click START.bat or serve\chat.bat instead.
  echo.
  python "serve\arkion-cli.py" --help
  echo.
  pause
  exit /b 0
)
python "serve\arkion-cli.py" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" pause
exit /b %RC%
