@echo off
REM ============================================================================
REM  _serve_env.bat  --  ASCII-only executable copy.
REM
REM  Why ASCII only: cmd parses < > | & (and quotes) even inside REM lines, and on a
REM  fresh CP936 console the UTF-8 Chinese comments were byte-misparsed, injecting stray
REM  operators -- the batch structure broke and the window closed instantly (2026-09-30).
REM
REM  Full Chinese notes (original file, verbatim):
REM      docs/_bat_originals/_serve_env.bat.txt
REM  Keep this file ASCII-only when editing. Do not paste Chinese text into it.
REM ============================================================================
REM ============================================================
REM
REM      call "%~dp0_serve_env.bat"
REM
REM ============================================================
REM [BAT-UX 2026-10-01] Guard against "I double-clicked it".  This file is a
REM   FRAGMENT: every launcher does `call _serve_env.bat` and expects it to do
REM   nothing but set variables.  Launched on its own it prints nothing, exits in
REM   milliseconds and the window closes -- which reads as a crash.
REM   %cmdcmdline% holds the command line of the thing that was launched, so this
REM   file's own name only appears there when THIS file is what got launched.
echo %cmdcmdline% | find /i "_serve_env.bat" >nul 2>&1
if not errorlevel 1 (
  echo [serve_env] This is NOT a launcher -- it is a shared settings file.
  echo [serve_env]
  echo [serve_env] The startup scripts each do "call _serve_env.bat" to pick up
  echo [serve_env] the engine tuning defaults.  On its own it only sets variables,
  echo [serve_env] so it finishes instantly and the window closes by itself.
  echo [serve_env]
  echo [serve_env] Use one of these instead:
  echo [serve_env]   START.bat             first-run wizard plus chat
  echo [serve_env]   serve\chat.bat        chat, starting the engine if needed
  echo [serve_env]   serve\run_serve.bat   run only the local server
  echo [serve_env]   run_gate_k3.bat       engine self-check
  echo [serve_env]   serve\cli.bat status  ask a running server what it is doing
  echo [serve_env]
  echo [serve_env] Press any key to close this window.
  pause >nul
  exit /b 0
)


REM [LOCATE 2026-10-01] Machine/package override written by
REM   `python serve\locate_model.py --write`; sourced BEFORE every if-not-defined
REM   below, so its values win over this file's defaults.  The generated
REM   arkion.local.bat sets ARK_MODEL_DIR (and optionally K4_PACK_DIR).
REM *** ASCII ONLY -- THE THREE LINES THAT USED TO SIT HERE WERE CHINESE AND THEY BROKE
REM *** THIS VERY CALL.  cmd decodes a .bat in the OEM codepage (CP936 here); the UTF-8
REM *** bytes shifted the character boundaries, the REM keyword was swallowed, the line
REM *** `arkion.local.bat` was executed as a command (it does not exist) and the CALL
REM *** below lost its `if exist "%~dp0ar` prefix.  Net effect: the machine override
REM *** NEVER loaded, ARK_MODEL_DIR silently fell back to the author's E:\qw38 and every
REM *** launcher died on a user's machine.  Do not paste Chinese into this file.
if exist "%~dp0arkion.local.bat" call "%~dp0arkion.local.bat"

if not defined ROPE_HOIST set ROPE_HOIST=1
if not defined GEMV_REGS  set GEMV_REGS=1

if not defined K4_CPSTREAM set K4_CPSTREAM=0
if not defined NSPLIT       set NSPLIT=5
if not defined K4ION        set K4ION=6
if not defined PRIORITY     set PRIORITY=2

if not defined K4_HB      set K4_HB=1
if not defined K4_SB      set K4_SB=1
if not defined K4_HBOT    set K4_HBOT=4
if not defined K4_RTB_LZ  set K4_RTB_LZ=1

if not defined K4_POOL       set K4_POOL=0
if not defined K4_PFVRAM     set K4_PFVRAM=0
if not defined K4_PFCOARSE   set K4_PFCOARSE=0
if not defined K4_POOL_CH    set K4_POOL_CH=256
if not defined K4_DMB        set K4_DMB=0
if not defined K4_DMB_MASK   set K4_DMB_MASK=32
if not defined K4_INCRIO     set K4_INCRIO=0
if not defined K4_RING       set K4_RING=0
if not defined K4_GEMMB      set K4_GEMMB=0
if not defined K4_MMA_CHK    set K4_MMA_CHK=0
if not defined K4_MMA_SMALLO set K4_MMA_SMALLO=0
if not defined K4_ST2        set K4_ST2=0
if not defined K4_BTNP       set K4_BTNP=0
if not defined QKV_FUSE      set QKV_FUSE=0
if not defined SPIN_WAIT     set SPIN_WAIT=0
if not defined RTB_PH        set RTB_PH=0
