@echo off
REM ============================================================================
REM  run_chat_k3.bat  --  ASCII-only executable copy.
REM
REM  Why ASCII only: cmd parses < > | & (and quotes) even inside REM lines, and on a
REM  fresh CP936 console the UTF-8 Chinese comments were byte-misparsed, injecting stray
REM  operators -- the batch structure broke and the window closed instantly (2026-09-30).
REM
REM  Full Chinese notes (original file, verbatim):
REM      docs/_bat_originals/run_chat_k3.bat.txt
REM  Keep this file ASCII-only when editing. Do not paste Chinese text into it.
REM ============================================================================
REM ============================================================
REM  run_chat_k3.bat - K3 build chat generation (same usage as run_chat.bat)
REM  Usage: run_chat_k3.bat "your question"
REM         run_chat_k3.bat "your question" 300
REM  NOTE: ASCII-only comments (cmd mis-decodes UTF-8 in .bat).
REM ============================================================
setlocal
cd /d "%~dp0"

set Q=%~1
if "%Q%"=="" set Q=What is the capital of France? Answer with the city name only.
set N=%~2
if "%N%"=="" set N=300

python tools\_mkprompt.py "%Q%" "%TEMP%\qw38_prompt_ids_k3.txt"
if errorlevel 1 (echo [err] prompt generation failed & exit /b 1)

set CHAT=1
set GEN_MAX=%N%
set TOPK=1
set QTEMP=1.0
set TOPP=0.95
set K4_ALL=1
set PLE_GPU=1
set RELEASE_CODE=1
set WIDE_GEMV=1
REM K3 production capacity (measured 2026-09-24: _t_129/_t_130/_t_131):
REM   Step 1 (_t_129): 25% smaller slots free ~1.5 GB RAM + 0.2 GiB VRAM. cap52/hot8 =
REM   Step 2 (_t_131): the next wall is nested. (a) our own gate ENT_RAM_FRAC=0.38 caps the
REM     pool at 15.62GB*0.38 = 5.94 GiB = 3418 slots (71/layer, silently clamped);
REM     (b) behind it the OS non-paged pool caps at 3623 slots = 6.29 GiB - the engine
REM     prints "prealloc pinned pool failed at 3623/4608 (nonpaged pool limit)" and
REM     downgrades K4CAP automatically. With the gate raised to 0.50 we get 3623 slots and
REM     72.7 ms/pos (-4.1% vs the 3418-slot control 75.8), miss 5.7%.
REM        (no failed top-up, no lazy fallback).
REM   To break the 6.29 GiB non-paged ceiling the engine has an unused switch: PAGEABLE_CACHE=1
REM     ("cache uses plain malloc, not limited by the non-paged pool") at the cost of slower
REM     H2D from pageable memory. Not yet measured.
REM   PPL is bit-identical (4.7105 / mean_logprob -1.549796) in every arm above.
set ENT_RAM_FRAC=0.50
set K4CAP=75
set K4HOT=10
REM [2026-09-25] K4_ZCPASS=1 now in BASE (device-side gating of miss H2D; bit-neutral, verified):
set K4_ZCPASS=1
set K4_HB=1
set K4_SB=1
set K4_HBOT=4
set K4_RTB_LZ=1
set PROMPT_FILE=%TEMP%\qw38_prompt_ids_k3.txt

call "%~dp0_serve_env.bat"
bin\arkion-q1.exe "E:/qw38" "%~dp0data\golden" > _chat_k3.log 2>&1

python tools\_decode.py qw38_chat_tokens.txt
echo.
echo full log: _chat_k3.log
endlocal
