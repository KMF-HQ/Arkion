@echo off
REM ============================================================================
REM  START.bat -- the single entry point. Double-click this.
REM
REM  All the logic lives in serve\first_run.py (Python handles the interactive
REM  prompts, unicode output and error handling far more safely than cmd).
REM  This file is deliberately tiny and ASCII-only on purpose: cmd mis-decodes
REM  UTF-8 comments in .bat files and mis-parses ( ) < > | & inside blocks.
REM ============================================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 goto :nopy

python "serve\first_run.py" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" pause
exit /b %RC%

:nopy
echo [err] Python not found in PATH.
echo.
echo   Install Python 3.8 or newer, and during setup tick
echo   "Add python.exe to PATH".  Then double-click START.bat again.
echo.
echo   Download: https://www.python.org/downloads/windows/
echo.
pause
exit /b 1
