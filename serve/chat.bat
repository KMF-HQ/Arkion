@echo off
REM ============================================================================
REM  serve/chat.bat -- one-click interactive chat with Arkion-Q1 (K3 engine).
REM
REM  ASCII-ONLY BY POLICY.  cmd parses < > | & ( ) and quotes even inside REM
REM  lines, and on a fresh CP936 console UTF-8 Chinese comments were
REM  byte-misparsed, injecting stray operators -- the batch structure broke and
REM  the window closed instantly (2026-09-30).  Keep this file ASCII-only.
REM
REM  This file was 0 bytes (never written).  Rewritten 2026-10-01.
REM
REM  What it does:
REM    1) probe http://127.0.0.1:PORT/healthz
REM    2) if the serve layer is not up, launch serve\run_serve.bat in a SEPARATE
REM       window and poll until /healthz answers  --  the engine takes ~45 s to
REM       load; chat_cli.py itself waits for the "started" flag after that.
REM    3) hand the console to serve\chat_cli.py  --  interactive REPL with
REM       streaming output, reasoning/content split, and /help /think /clear
REM       /temp /maxtok /system /save commands.
REM
REM  Usage:
REM    serve\chat.bat                      interactive
REM    serve\chat.bat --once "1+1=?"       one shot, then exit
REM    serve\chat.bat --no-think           no reasoning -- faster, for tool tasks
REM    serve\chat.bat --max-tokens 512     cap the generation
REM    set ARKION_PORT=8472  and  serve\chat.bat   for a non-default port
REM
REM  Known timings on this box, 2026-10-01 (8 GB VRAM / 16 GB RAM):
REM    first token ~25 s for a ~300-token prompt  --  that is the dense prefill,
REM      not the serving layer.  The default think template alone is ~200 tokens
REM      of system prompt, and P0 is stateless so every new session pays it.
REM    generation ~83-98 ms/token (streaming).
REM    SERV_CTX default is 65536, the largest VERIFIED value here.  131072 kills
REM      the engine on the very first request -- see the CTX-FIX note in
REM      run_serve.bat before raising it.
REM
REM  The serve layer is deliberately LEFT RUNNING when you quit, so the next
REM  chat.bat starts instantly.  To stop it: close the "arkion-serve" window, or
REM    taskkill /IM arkion-q1.exe /F
REM ============================================================================
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8

set PORT=%ARKION_PORT%
if "%PORT%"=="" set PORT=8471
set URL=http://127.0.0.1:%PORT%

python -c "import urllib.request;urllib.request.urlopen('%URL%/healthz',timeout=3)" >nul 2>&1
if not errorlevel 1 goto :ready

echo [chat.bat] no server on %URL% -- starting the engine now.
echo [chat.bat] It takes about 45 s; progress appears in the new "arkion-serve" window.
start "arkion-serve" cmd /c "serve\run_serve.bat %PORT%"

set /a TRIES=0
:wait
set /a TRIES+=1
python -c "import urllib.request;urllib.request.urlopen('%URL%/healthz',timeout=3)" >nul 2>&1
if not errorlevel 1 goto :ready
if %TRIES% GEQ 150 goto :timeout
if %TRIES%==15 echo [chat.bat] still loading, please wait ...
ping -n 3 127.0.0.1 >nul 2>&1
goto :wait

:timeout
echo [chat.bat] FATAL: %URL% did not answer after about 5 minutes.
echo [chat.bat] Look at the "arkion-serve" window for the engine error.
echo [chat.bat] A dead engine on the first request usually means SERVER_CTX is
echo [chat.bat] too large -- read the CTX-FIX note at the top of run_serve.bat.
pause
exit /b 1

:ready
echo [chat.bat] server ready at %URL%  --  type /help for commands, /exit to quit.
python "serve\chat_cli.py" --url "%URL%" %*
set RC=%ERRORLEVEL%
echo.
echo [chat.bat] chat closed. The serve layer is still running; just run
echo [chat.bat] chat.bat again for an instant start. To free the GPU:
echo [chat.bat]   taskkill /IM arkion-q1.exe /F
exit /b %RC%
