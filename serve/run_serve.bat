@echo off
REM ============================================================================
REM  serve/run_serve.bat  --  ASCII-only executable copy.
REM
REM  Why ASCII only: cmd parses < > | & (and quotes) even inside REM lines, and on a
REM  fresh CP936 console the UTF-8 Chinese comments were byte-misparsed, injecting stray
REM  operators -- the batch structure broke and the window closed instantly (2026-09-30).
REM
REM  Full Chinese notes (original file, verbatim):
REM      docs/_bat_originals/serve_run_serve.bat.txt
REM  Keep this file ASCII-only when editing. Do not paste Chinese text into it.
REM ============================================================================
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0.."

set K4_ALL=1
set PLE_GPU=1
set RELEASE_CODE=1
set WIDE_GEMV=1
set K4_MMA=1
set K4_MMA_GDN=1
set K4_MMA_ATTN=1
set K4_HB=1
set K4_SB=1
if not defined K4_UNIQ set K4_UNIQ=1
if not defined K4_XLPOS set K4_XLPOS=1
set K4_RTB_LZ=1
set ROPE_HOIST=1
set GEMV_REGS=1
set F8_VEC=0
set GEMV_RB_THR=1024
set XL_K=4
set XL_TOPK=4
set XL_D=1
set XL_PRATIO=0
set XL_PF_TAU=0.005
set XL_PFMAX=0
set XL_SORTKEY=1
set K4_XLFIX=1
REM [2026-10-01 POOL-FIX] The three knobs below USED TO BE 8 / 95 / 0.55 and the
REM   launcher could not start on a 16 GB-RAM box: ENT_RAM_FRAC=0.55 asks for
REM   8.59 GB of pinned, the measured hard ceiling on this machine is ~7.0 GiB,
REM   so the prealloc ate the whole budget and the very next small pinned alloc
REM   failed with `cudaMallocHost(h_pred[_j], 2048 B): out of memory`.
REM   Measured in chat mode, same exe, 282 tokens, official sampling:
REM     FRAC 0.50 / K4CAP 75 / K4HOT 0  ->  84.2 ms/tok   (this line)
REM     FRAC 0.55 / K4CAP 95 / K4HOT 8  ->  startup OOM
REM   K4HOT=0 is the 8 GB-safe choice; a 24 GB card may profit from 8 (that is
REM   a VRAM knob and is NOT related to the host-side OOM above).
set K4HOT=0
set K4_B3RES=0
set K4_MOEGRAPH=1
set GR_RB=0
set K4CAP=75
set ENT_RAM_FRAC=0.50
set K4_SKIP_COLD=0
if not defined K4_SKIP_TAU set K4_SKIP_TAU=0.02
set K4_HBOT=4
set NSPLIT=5
set K4ION=6
set PRIORITY=2
set K4_CPSTREAM=0
set K4_ZCPASS=0
set K4_POOL=0
set K4_PFVRAM=0
set K4_PFCOARSE=0
set K4_POOL_CH=256
set K4_DMB=0
set K4_DMB_MASK=32
set K4_INCRIO=0
set K4_RING=0
set K4_GEMMB=0
set K4_MMA_CHK=0
set K4_MMA_SMALLO=0
set K4_ST2=0
set K4_BTNP=0
set RTB_PH=0
set SPIN_WAIT=0
set QKV_FUSE=0
set K4_MTP_BATCH=0
set K4_QSA=1
REM [2026-10-01 CTX-FIX] WAS 131072 and that value **kills the engine** on this
REM   box (8 GB VRAM / 16 GB RAM): first request dies inside the KV-reserved
REM   build with the log stopping at `[M4] GR2 done, enter gate li=0`, no error
REM   line, process simply gone -- the HTTP client sees an empty 200 stream.
REM   Reproduced twice (with and without ARKION_KV_SYS_SNAP).
REM   Same exe, same env, only SERVER_CTX=4096 => works end to end:
REM     ttft 22.7s ( = 297-token prefill @ ~76 ms/tok ), 129 tokens, 89.3 ms/tok.
REM   So the OpenAI-compatible surface was BLOCKED BY THE KV RESERVE SIZE, not
REM   by the serving layer.
REM   SCANNED (2026-10-01, _t_ctx_scan.py, one short request per value on this
REM   8 GB VRAM / 16 GB RAM box):
REM     4096 8192 16384 32768 **65536**  -> all req_ok (17 tok, engine alive)
REM     131072                            -> engine gone on the first request
REM   65536 is therefore the largest VERIFIED value here; 131072 is an isolated
REM   fatal point, not a gradual cliff. Raise further via ARKION_CTX=<rows> only
REM   after re-running _t_ctx_scan.py on the target machine.
set SERVER_CTX=65536
if defined ARKION_CTX set SERVER_CTX=%ARKION_CTX%
if not defined K4_KV_Q2 set K4_KV_Q2=1
if not defined K4_KVTIER set K4_KVTIER=1
if not defined K4_KVW set K4_KVW=2048
if not defined K4_NT set K4_NT=1
if not defined K4_PFB set K4_PFB=0
set K4_MTP=0
if not defined ARKION_KV_REUSE set ARKION_KV_REUSE=1
if not defined SNAP_BUDGET_MB set SNAP_BUDGET_MB=3072
if not defined ARKION_KV_DISK set ARKION_KV_DISK=1
if not defined ARKION_SNAP_DISK_MB set ARKION_SNAP_DISK_MB=131072
if not defined ARKION_SNAP_ROOT set ARKION_SNAP_ROOT=%~dp0..\_snaps
if not defined ARKION_KV_SYS_SNAP set ARKION_KV_SYS_SNAP=1
if not defined ARKION_KV_SYS_SCOPE set ARKION_KV_SYS_SCOPE=sys+user

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set ARKION_LOG_PASSTHROUGH=1
if not defined ARKION_ENGINE_LOG set ARKION_ENGINE_LOG=_engine.log
set ARKION_HOST=127.0.0.1
if not "%~1"=="" (set ARKION_PORT=%~1) else (set ARKION_PORT=8471)

REM ============================================================================
REM  PATHS [PATH-ENV 2026-10-01] -- the engine no longer has ANY baked-in absolute
REM  path, so these must describe YOUR layout.  Edit the three defaults below.
REM    ARK_MODEL_DIR  model dir. REQUIRED: the exe now refuses to guess (it used to
REM                   silently fall back to E:/qw38, which is how "works on my box"
REM                   turned into "load fail" somewhere else).  argv[1] wins.
REM    ARK_PLE_ROOT   the original bf16 repo holding the PLE shards (~350 GB). This
REM                   is a SEPARATE artifact from the model dir.  Reading the wrong
REM                   one does NOT error out -- it decodes to garbage (relRMS 0.996,
REM                   14/16 gate, pos11 flipped), which is why the engine now prints
REM                   the resolved path and its source at startup.
REM    QWEN_DIR       tokenizer.json / chat_template.jinja, used by serve/tokens.py.
REM                   Defaults to ARK_PLE_ROOT (same repo).
REM ============================================================================
REM [LOCATE 2026-10-01] Source the machine override FIRST (`serve\locate_model.py
REM   --write` generates it); its values win over the defaults further down.
if exist "%~dp0..\arkion.local.bat" call "%~dp0..\arkion.local.bat"

REM [PATH-ENV 2026-10-01] NO baked-in dev paths any more.  This file used to fall back
REM   to the author's E:\qw38 / D:\qwen3.8-flash-next.  On any other machine that is
REM   simply a wrong path: the engine died with a FATAL the user could not explain
REM   ("why is it looking at E:\qw38?") and the console window vanished.  Fail early
REM   and say what to do instead.
if not defined ARK_MODEL_DIR (
  echo [run_serve] FATAL: ARK_MODEL_DIR is not set and arkion.local.bat was not found.
  echo [run_serve]   Easiest fix: run START.bat once -- the wizard locates the model
  echo [run_serve]   and writes arkion.local.bat in the folder above serve\.
  echo [run_serve]   Manual fix:
  echo [run_serve]     set ARK_MODEL_DIR=E:\path\to\model
  echo [run_serve]     set ARK_PLE_ROOT=D:\path\to\qwen3.8-flash-next
  if not defined ARK_NO_PAUSE pause
  exit /b 2
)
if not exist "%ARK_MODEL_DIR%\nonexp_pack.json" (
  echo [run_serve] FATAL: %ARK_MODEL_DIR%\nonexp_pack.json was not found.
  echo [run_serve]   ARK_MODEL_DIR must be the folder holding nonexp_pack.json,
  echo [run_serve]   nonexp_fp8.bin / nonexp_bf16.bin, ple_index.json and the expert
  echo [run_serve]   packs -- see the "what you need" table in README.md.
  if not defined ARK_NO_PAUSE pause
  exit /b 2
)
if not defined ARK_PLE_ROOT (
  echo [run_serve] WARNING: ARK_PLE_ROOT is not set, so the engine will use its
  echo [run_serve]   built-in fallback -- the AUTHOR's path, which will not exist on
  echo [run_serve]   your machine.  Point it at the repo holding the PLE shards.
  echo [run_serve]   PLE shards are the ~335 GB original bf16 repo, NOT the model dir.
)
if defined ARK_PLE_ROOT if not defined QWEN_DIR set QWEN_DIR=%ARK_PLE_ROOT%

echo [run_serve] KV reserve=%SERVER_CTX% rows ^| http://%ARKION_HOST%:%ARKION_PORT%
echo [run_serve] model=%ARK_MODEL_DIR%
echo [run_serve] ple_root=%ARK_PLE_ROOT%  qwen_dir=%QWEN_DIR%
python "%~dp0app.py"
set RC=%ERRORLEVEL%
endlocal & set RC=%RC%

REM [BAT-UX 2026-10-01] The server blocks here while it runs, so the window normally
REM   stays up.  If it returns immediately -- python missing, port already taken, the
REM   engine refusing to start -- hold the window open so the reason is readable
REM   instead of the window vanishing.  ARK_NO_PAUSE=1 skips this for automation.
if not "%RC%"=="0" (
  echo.
  echo [run_serve] the serve layer exited with code %RC%.
  echo [run_serve] the reason is in the lines above, and in _engine.log.
  if not defined ARK_NO_PAUSE pause
)
