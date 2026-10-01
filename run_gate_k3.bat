@echo off
REM ============================================================================
REM  run_gate_k3.bat  --  ASCII-only executable copy.
REM
REM  Why ASCII only: cmd parses < > | & (and quotes) even inside REM lines, and on a
REM  fresh CP936 console the UTF-8 Chinese comments were byte-misparsed, injecting stray
REM  operators -- the batch structure broke and the window closed instantly (2026-09-30).
REM
REM  Full Chinese notes (original file, verbatim):
REM      docs/_bat_originals/run_gate_k3.bat.txt
REM  Keep this file ASCII-only when editing. Do not paste Chinese text into it.
REM ============================================================================
REM ============================================================
REM  run_gate_k3.bat - K3 build gate (same criteria as run_gate.bat, exe/pack swapped)
REM    exe  : bin\arkion-q1.exe  (-DK4_BITS=3, default path = k_k4_gemv_flip, orientation B)
REM    pack : experts_k3c (calibrated, 24576/24576 slots; default dir for K4_BITS=3)
REM  Expect: === M3 GPU end-to-end gate: 15/16 PASS === and pos19 argmax=109455
REM  NOTE: comments in this file are ASCII-only on purpose.
REM        cmd decodes .bat in the OEM codepage (GBK here); UTF-8 Chinese comments
REM        get mis-decoded and some lines get executed as commands ("'6' is not
REM        recognized..."), which is harmless noise but can break the script.
REM ============================================================
setlocal
cd /d "%~dp0"

set K4_ALL=1
set PLE_GPU=1
set RELEASE_CODE=1
set WIDE_GEMV=1
set XL_K=4
set XL_D=1
set XL_PRATIO=0
set XL_PF_TAU=0.005
set PRIORITY=2
set K4ION=6
set K4_CPSTREAM=0
set K4CAP=52
set ENT_RAM_FRAC=0.38
set K4_SKIP_COLD=2
set K4HOT=8
set K4_B3RES=1
set K4_SKIP_TAU=0.02
set K4_MOEGRAPH=1
set GR_RB=1
set ROPE_HOIST=1
set GEMV_REGS=1
set SPIN_WAIT=0
set K4_ZCPASS=1
set K4_HB=1
set K4_SB=1
set K4_HBOT=4
set K4_RTB_LZ=1
REM [2026-09-25] K4_ZCPASS=1 now in BASE. Device-side gating: the copy engine starts a miss
REM   slot's H2D the moment its disk read lands, with no CPU round-trip through io_wait_slot.
REM   (4.7105 / mean_logprob -1.549796), gate unchanged. The saved time shows up inside
REM   cp_end until the transport leaves the critical path, so the wall gain is ~2.4 ms today.
REM do NOT set CHAT / GEN_MAX here (gate runs in chain mode)

call "%~dp0_serve_env.bat"

REM [PATH-ENV 2026-10-01] WAS a hardcoded "E:/qw38" (the author's drive).  The gate is
REM   the one tool that proves an install is sane, so it has to work on the machine it
REM   runs on: take the model dir from arkion.local.bat / ARK_MODEL_DIR and refuse to
REM   guess.  (The engine refuses too now -- it exits 2 with a readable message.)
if not defined ARK_MODEL_DIR (
  echo [gate] FATAL: ARK_MODEL_DIR is not set.  Run START.bat once, or set it manually.
  if not defined ARK_NO_PAUSE pause
  exit /b 2
)
if not exist "%ARK_MODEL_DIR%\nonexp_pack.json" (
  echo [gate] FATAL: %ARK_MODEL_DIR%\nonexp_pack.json was not found.
  echo [gate]   ARK_MODEL_DIR must point at the model folder -- see README.md.
  if not defined ARK_NO_PAUSE pause
  exit /b 2
)
bin\arkion-q1.exe "%ARK_MODEL_DIR%" "%~dp0data\golden" > _gate_k3.log 2>&1

echo.
echo === K3 gate result ===
findstr /C:"M3 GPU" /C:"pos 19" /C:"CUDA ERR" /C:"FATAL" _gate_k3.log
echo.
echo full log: _gate_k3.log
REM [BAT-UX 2026-10-01] Hold the window open when double-clicked.  Automation can set
REM   ARK_NO_PAUSE=1 to skip the prompt.
if not defined ARK_NO_PAUSE pause
endlocal
