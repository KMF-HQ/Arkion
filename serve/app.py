#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve/app.py — Arkion-Q1 服务入口（P0 无状态 OpenAI 兼容面）

跑法:
    python serve/app.py                     # 真引擎（默认 0.0.0.0:8471）
    set ARKION_MOCK=1&& python serve/app.py # Mock 引擎，不需要 GPU/磁盘（联调用）
    python serve/app.py --selftest          # 离线自测（Mock + TestClient，不占端口）

端点:
    POST /v1/chat/completions   (stream / non-stream)
    GET  /v1/models
    GET  /healthz

P0 明确的范围（写进边界，不要越界）:
    · 无状态：每轮把全量 messages 重喂，引擎侧每请求重置会话（KV 不从轮间复用）
    · 单并发：引擎侧一次一个 GEN，其余排队（性能线的 K4_NT 批处理未接入）
    · 工具调用：本层只把模型输出的 XML 原样放在 content 里；解析/回灌在 P1
    · 不支持（显式 400）：n>1、logprobs、非 0 penalty、tool_choice 非 auto/none、多模态输入

引擎侧状态（2026-09-27）：**SERVER=1 常驻协议已实现并联调通过**（READY/TOK/END/PONG/ERR +
请求级采样参数 + 每请求会话态重置；见 src/helm_qw38_gpu2.cu 的 [SERVER] 段）。
常驻收益：一次性模式每轮 ~14s（~10s 启动 + 生成），常驻后第二轮起仅生成时间（~3.6s）。
仍需注意：引擎 KV 容量在启动期定格（SERVER_CTX，默认 4096 行）⇒ prompt+max_new+1 不得超限。
"""
import json
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from api import (APIError, BASE_MODEL, DONE, ENGINE_TAG,  # noqa: E402
                 DEFAULT_MAX_NEW, MODEL_ID, ChatCompletionRequest, StopFilter, chunk, models_body,
                 response_body, usage_chunk, validate)
from context import ContextManager, _key           # noqa: E402  [KVREUSE] 快照索引/LRU/前缀匹配
from engine import EngineError, make_engine          # noqa: E402
from think import ThinkSplitter                        # noqa: E402
from tokens import PromptError, Tokens                 # noqa: E402

# ---- [KVREUSE 2026-09-27] 跨请求 KV 前缀复用 ----
#   引擎侧：SNAP/RESTORE/GENS 三个协议 op（会话态整块存 RAM）。serve 侧：只做索引 ——
#   最长前缀匹配（context.ContextManager.lookup）+ LRU/预算。所有失败路径都降级为全量 prefill。
#   ⚠ 与 K4_B3RES / K4_SKIP_COLD 不兼容（实测：续跑与全量重跑会在后续 token 处分叉，
#     因为 B3 回插与 COLD 判据依赖的缓存状态无法随会话快照搬运）⇒ 生产配置里关掉这两项。
# ---- [KV-DISK 2026-09-29] 快照永久落盘（跨服务重启存活）----
#   动机：prefill 是 76 ms/token **严格线性**（128K ≈ 2.8 h），而落盘读回一份 1.5 GB ≈ 0.3 s
#         ⇒ 差 4 个数量级。原先 SNAP/RESTORE 只存活在引擎进程内，重启即全丢。
#   做法：走引擎的 SNAPF/RESTOREF（显式文件路径），磁盘预算/LRU/索引在本层（context.py）：
#     · 文件 root/<前缀指纹>.bin；index.json 持久化前缀 → 可跨重启做最长前缀匹配。
#     · 引擎侧只做格式与**指纹**校验（编译戳 + 数值开关白名单 + 数据根路径），不符即 ERR ⇒
#       本层降级全量 prefill。指纹不同 ⇒ 天然 miss，但**旧缓存不删**（切回开关仍能命中）。
#   ⚠ 单上下文约束：引擎只有一个 KV 上下文槽 ⇒ 「按需激活」是一次搬运（一次约 0.4 s），
#     并发请求会互相踢。当前单用户/低并发场景无碍；要真并发需多上下文槽（另立项）。
KV_REUSE = os.environ.get("ARKION_KV_REUSE", "1") != "0"
KV_DISK = KV_REUSE and os.environ.get("ARKION_KV_DISK", "1") != "0"
_SNAP_ROOT = os.environ.get("ARKION_SNAP_ROOT", os.path.join(_HERE, "_snaps"))
# 磁盘预算（默认 128 GiB；128K 一份约 1.57 GB ⇒ 约 80 份）。E: 实测剩 297 GB。
_SNAP_DISK_MB = int(os.environ.get("ARKION_SNAP_DISK_MB", "131072"))
CTX = ContextManager(_SNAP_ROOT,
                     budget_bytes=_SNAP_DISK_MB << 20,
                     backend=None,          # 字节由引擎 SNAPF/RESTOREF 真实落盘，这里只索引
                     min_ctx=int(os.environ.get("ARKION_KV_MIN_PREFIX", "16")))
if KV_REUSE:
    try:
        _n_idx = CTX.load_index()      # 重启后把前缀 → 快照文件的映射捞回来（这才是"命中存活"的前提）
        print("[kv] disk index: %d snapshots, %.1f MB / %d MB (root=%s)"
              % (_n_idx, CTX.used_bytes / 1e6, _SNAP_DISK_MB, _SNAP_ROOT))
    except Exception as _e:            # noqa: BLE001 索引坏了不该拦住服务
        print("[kv] disk index load failed (%s) -> 从空索引开始" % _e)
_CTX_SNAPS_SEEN = set()      # 已建立过快照的 prefix 指纹（避免重复 prefill system）
# ---- [PRE / 系统前缀快照 2026-09-29] 跨会话复用的唯一支点 ----
#   为什么它是第一优先级：Agent 平台的「系统提示词 + 记忆」常有 **30K token**，冷启动按
#   76 ms/token 算是 **≈38 分钟**；而这条前缀对**每个新会话都相同** ⇒ 拍一次，之后所有以它
#   开头的请求都命中（读盘恢复 ≈0.1~0.3 s）。这与「同会话多轮」的 (b) 快照是两件事。
#   过去它默认关闭，因为唯一可用的手段是 generate(sys, 0) —— 而 engine.py 把 max_new<=0 解释为
#   「按剩余 KV 补满」⇒ 会真的生成一大段、拍下的快照 = sys + 生成内容（kvn 158 ≠ sys 61）✗。
#   现在改用 `S.engine.prefill()`（发字面 max_new=0：引擎侧 PMAX=NPRE+0 ⇒ 跑完 prompt 即停）
#   ⇒ 语义干净，实测「sys 前缀复用 == 全量重跑」逐位相同（_t_pre_sys.py）。
#   代价：每个（构建 + 配置）指纹下**只需付一次** sys prefill；之后落盘存活、重启零成本。
KV_SYS_SNAP = os.environ.get("ARKION_KV_SYS_SNAP", "1") != "0"
# 固定前缀的范围：
#   sys+user（默认）= [system..., 首个 user]（沿用原行为：用户常把长材料放第一条）
#   sys            = **只取第一条 system** —— Agent 平台口径：静态系统提示词在前、
#                    动态记忆作为**第二条 system**或首条 user 跟在其后 ⇒ 每个会话都能命中静态段。
#   ⚠ 命中前提是「静态段逐字节不变」：不要在静态 system 里塞时间戳/会话 id；记忆请另起一条消息。
KV_SYS_SCOPE = os.environ.get("ARKION_KV_SYS_SCOPE", "sys+user").strip().lower()
# 启动预热：把系统提示词文本放这里（一个 .txt），服务启动后自动 prefill + 落盘该前缀，
#   这样平台发来第一个请求时就已经是命中态（否则那个请求要白等一次 30K prefill）。
#   已有缓存（含重启后从 index.json 恢复）时跳过 ⇒ 只付一次。
SYS_PROMPT_FILE = os.environ.get("ARKION_SYS_PROMPT_FILE") or ""
# [PRE-WARM 2026-09-30] 命中失败时**先预热固定前缀、再生成**。
#   为什么必须补这一刀：原实现是「回合结束才拍 (a) 快照」，而 Agent 平台的 prompt 常达 70K token
#   （按 76 ms/token 约 89 分钟）⇒ 第一轮根本跑不完就被取消/超时，快照永远建不起来。
#   实测症状：连续 3 个请求全部 `lookup 未命中 -> 全量 prefill`，每个都白烧 100 分钟。
#   预暖 = 先 prefill(固定前缀) + 落盘快照，再 restore 回来继续本轮 ⇒ 本轮总代价不变，
#   但**保证第一轮之后就一定存在快照**（后续请求命中）。任何失败都降级为全量 prefill。
KV_WARM = os.environ.get("ARKION_KV_WARM", "1") != "0"
# [KV-WARM-MIN 2026-09-30] 默认 2048 -> **256**
#   原值的经济账是错的：PRE-WARM 的那次 prefill **不是额外开销**（它同时是本轮续跑的起点），
#   真正的边际成本只有「写一份快照 ~0.06 s（162 MB / 2.9 GB/s）+ restore ~0.1~0.3 s」，
#   而收益是此后每个同前缀请求省掉 k x 76 ms：
#     k=64  => 省 4.9 s/次；k=484 => 省 36.8 s/次 —— 都远大于约 0.4 s 的成本。
#   [!] 更严重的是 2048 这个门限造成的**实际惩罚**：(16, 2048) 区间内 PRE-WARM 不触发，
#     但收尾那条 (a)（app.py:_snap_sys_prefix「首请求」）仍会照跑 => 那一轮被 prefill 了**两次**。
#     实测（_t_kvcache_srv.py，484 token 前缀）：首轮 78.2 s = 主路径 36.5 s + (a) 37.6 s；
#     放开后首轮应为 ~42 s（省一半），第 2 轮 5.7 s 不变。
#   [!] 代价必须记账：每遇到一个**不同的首条 user**就写一份 162 MB（递推态 S 本身 151 MB，躲不掉）
#     => 若你的首条消息从不复用，把这个值调回 2048，或把 KV_SYS_SCOPE 设成 sys（只取静态段）。
KV_WARM_MIN = int(os.environ.get("ARKION_KV_WARM_MIN", "256"))

# ---- 默认思考模板 ----
#   当请求开启思考（reasoning_effort 不是 minimal）且未提供 system message 时，
#   自动注入此模板。用 ARKION_DEFAULT_THINK_TEMPLATE=0 关闭。
DEFAULT_THINK_TEMPLATE = """你是一个严谨的思考型助手。思考时严格遵守以下规则：

先分级：简单直接答，复杂走循环。
对齐目标、约束、验收、预算、权限。
涉及"之前"、旧决策、重复失败，先查记忆；无工具则声明。
事实、时效、高风险问题优先查工具，记来源、时间、置信度。
任务树只放骨架：ID、目标、状态、依赖、验收、证据、下一步。
用户新观点默认候选，不自动改主目标。
执行时稳全局、验局部；同一失败两次就停，换策略/查资料/问用户。
文档记时间戳、版本、状态、事实、假设、实验、结果、失败、决策、待办。
阶段审查：一致性、正确性、可用性、风险、成本、合规、可简化点。
不知道就说不知道；不确定给置信度和验证路径。
停止条件：验收达成、预算耗尽、需用户决策、连续失败、目标冲突。
执行纪律严格，审查适当发散；防漂移，防循环。
生成忠于思考，思考反思生成：正文要落实思考得出的结论与约束；一旦发现正文写不下去，
就回头修正思考再继续，而不是在思考中途就结束回合。"""
_DEFAULT_THINK_ON = os.environ.get("ARKION_DEFAULT_THINK_TEMPLATE", "1") != "0"

# ===== [THINK-GUARD 2026-09-28] 思考未闭合保护 =====
#   症状（实测）: 模型在思考段中途直接吐 <|im_end|>（引擎日志 `[chat] EOS 248046 at p=667`），
#     没写 `</think>` 就结束本回合 ⇒ ThinkSplitter 全程停在 reasoning 态 ⇒ 客户端只拿到一大段
#     思考、**正文为空**（用户报的「只有思考没有输出正文就结束了」）。
#   服务端不能伪造正文，但可以把这一回合**合法地接着写完**：引擎此刻的 KV 恰好停在
#     「思考末尾」（= ids + 已生成），于是复用快照机制 SNAP→RESTORE→`GENS`：先喂 `</think>`
#     把思考显式闭合，再让它继续生成正文。代价只有一次快照往返（短上下文毫秒级）。
#   THINK_CLOSER 必须与 serve/think.py 的 CLOSE 同源；"\n\n" 是按模板惯例补的分隔空行。
THINK_CLOSER = "</think>\n\n"
# 默认**开**（2026-09-28）：已按确定性路径验证通过 —— 触发时日志打
#   `[think] EOS 时思考未闭合 -> 回喂 N 个尾巴 token + 补 '</think>\n\n' 后从 p=... 续跑`，
#   正文由 0 字变为有内容、且不与思考段重复。关它用 ARKION_THINK_GUARD=0。
_THINK_GUARD = os.environ.get("ARKION_THINK_GUARD", "1") != "0"
# 仅用于确定性验证保护逻辑：强制在「被 max_tokens 截断」时也触发续跑。
_THINK_GUARD_FORCE = os.environ.get("ARKION_THINK_GUARD_FORCE", "0") == "1"
def _ilog(msg):
    """[THINK-GUARD] 诊断输出：走 stderr 且强制 flush。
    ⚠ 不能只用 print：stdout 是**块缓冲**，少量诊断行会被压在缓冲里看不见 —— 排查
      「保护为什么没触发」时曾据此误判成「条件为假」（实际只是打印没落盘）。"""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# 启动即回显这两个开关：排查「保护没触发」时，先要能确认它到底有没有被打开。
_ilog("[think-guard] enabled=%s force=%s closer=%r" % (_THINK_GUARD, _THINK_GUARD_FORCE, THINK_CLOSER))

@asynccontextmanager
async def _lifespan(_app):
    """服务启动时后台预热引擎（_spawn_warmup 定义在本模块后部，运行时已就绪）。"""
    _spawn_warmup()
    yield


app = FastAPI(title="Arkion-Q1 Serve", version="0.1.0", lifespan=_lifespan)


class Server:
    """Tokens / Engine 懒加载 + 单例。启动只做一次（多线程下加锁）。"""

    def __init__(self):
        self._tokens = None
        self._engine = None
        self._started = False
        self._lock = threading.Lock()

    @property
    def tokens(self):
        if self._tokens is None:
            self._tokens = Tokens()
        return self._tokens

    @property
    def engine(self):
        if self._engine is None:
            self._engine = make_engine(self.tokens)
        return self._engine

    def ensure_started(self):
        with self._lock:
            if not self._started:
                self.engine.start()
                self._started = True
                # [KV-DISK] 绑定引擎指纹 = 磁盘命名空间：换开关/重编译后旧缓存**不被覆盖**
                #   （切回原配置仍能命中），而当前配置只认自己命名空间的条目。
                try:
                    st = self.engine.fingerprint() if hasattr(self.engine, "fingerprint") else ""
                    CTX.set_stamp(st)
                    print("[kv] engine fingerprint=%s -> disk namespace" % (st or "-"))
                except Exception as e:                      # noqa: BLE001 指纹拿不到不该拦住服务
                    print("[kv] fingerprint 获取失败（%s）-> 空命名空间" % e)
        return self._engine


S = Server()


def _snap_sys_prefix(sys_ids, why=""):
    """[PRE 2026-09-29] 把「固定前缀」（系统提示词）单独拍成一条快照 —— 跨会话复用的支点。

    ★ 已缓存则直接返回（`CTX.exact`，含服务重启后从 index.json 恢复的情形）⇒ 重启零成本。
    返回登记的 dict；未拍（已存在/太短）返回 None。
    """
    if CTX.exact(sys_ids) is not None:
        print("[kv] (a) sys 前缀已在缓存中（%d token）-> 跳过 prefill" % len(sys_ids))
        return None
    t0 = time.time()
    S.engine.prefill(sys_ids)                       # 只 prefill，不生成（引擎 max_new=0 语义）
    s_tag = CTX.tag(sys_ids)
    if KV_DISK:
        nb, kvn = S.engine.snapf(s_tag, CTX.path_for(sys_ids))
    else:
        nb, kvn = S.engine.snap(s_tag)
    CTX.submit(sys_ids, nbytes=nb)
    if KV_DISK:
        CTX.save_index()
    _CTX_SNAPS_SEEN.add(s_tag)
    ok = (kvn == len(sys_ids))
    print("[kv] (a) sys 快照%s：sys=%d kvn=%d 一致=%s 体积=%.0f MB 用时=%.1fs"
          % (("［%s］" % why) if why else "", len(sys_ids), kvn, ok, nb / 1e6, time.time() - t0))
    if not ok:
        print("[kv] (a) [WARN] 快照长度 != sys 长度 ⇒ 该条不可用于复用（上层会安全拒绝）")
    return {"tag": s_tag, "kv": kvn, "bytes": nb, "n": len(sys_ids)}


def _sys_ids_from_text(txt):
    """从系统提示词**文本**推出它的 token 段（启动预热用）。

    本模型的 chat 模板不接受「只有 system」（报 No user query found），故用**差分法**：
    编码 [sys, 占位 user] 与 [占位 user]，长度差即 sys 段；再校验「后半段逐 token 相同」才采用
    （不成立就放弃 —— 宁可不算，也不要一个错前缀，因为错前缀会一直 MISS，浪费一次 30K prefill）。
    """
    _kw = dict(add_generation_prompt=False)
    _dummy = {"role": "user", "content": "x"}
    a = list(S.tokens.encode_messages([{"role": "system", "content": txt}, _dummy], **_kw))
    b = list(S.tokens.encode_messages([_dummy], **_kw))
    n = len(a) - len(b)
    if n <= 0 or a[n:] != b:
        print("[kv] boot: 差分推导失败（n=%d）-> 跳过预热（首个请求会走全量 prefill）" % n)
        return None
    return a[:n]


def _spawn_warmup():
    """后台预热引擎：把 ~10s 加载放到服务启动时，否则第一个请求要白等一遍。
    关闭：ARKION_NO_WARM=1；Mock 模式不预热；预热失败不阻塞服务（首个请求会重新报错）。"""
    # [KVREUSE] 配置检查：这两个开关会让「续跑」与「全量重跑」分叉（缓存/回插状态不可搬运）
    if KV_REUSE and (os.environ.get("K4_B3RES") not in (None, "0") or
                     os.environ.get("K4_SKIP_COLD") not in (None, "0")):
        print("[arkion-serve] WARN: KV 复用与 K4_B3RES/K4_SKIP_COLD 不兼容（续跑会与全量重跑分叉）"
              " —— 请在 run_serve.bat 设 K4_B3RES=0 K4_SKIP_COLD=0（K4_SKIP_TAU 可保留）")
    if os.environ.get("ARKION_NO_WARM") == "1" or os.environ.get("ARKION_MOCK") == "1":
        return

    def _w():
        try:
            S.ensure_started()
            print("[arkion-serve] engine warm: READY in %.1fs" % (S._engine.ready_seconds or -1))
            _boot_warm_sys()
        except Exception as e:      # noqa: BLE001 预热失败只告警
            print("[arkion-serve] warmup failed: %s" % e)

    threading.Thread(target=_w, daemon=True).start()


def _boot_warm_sys():
    """[PRE] 启动预热系统前缀（ARKION_SYS_PROMPT_FILE）。

    动机：平台发来第一个请求前，先把静态系统提示词 prefill 一次并落盘 ⇒ 那个请求就是命中态，
    不必白等一次（30K 约 38 分钟）。已有缓存（含重启后从索引恢复）时 _snap_sys_prefix 会跳过。
    """
    if not (KV_REUSE and KV_SYS_SNAP and SYS_PROMPT_FILE):
        return
    if not os.path.exists(SYS_PROMPT_FILE):
        print("[kv] boot: ARKION_SYS_PROMPT_FILE 不存在：%s" % SYS_PROMPT_FILE)
        return
    try:
        with open(SYS_PROMPT_FILE, encoding="utf-8", errors="replace") as fh:
            txt = fh.read().strip()
    except OSError as e:
        print("[kv] boot: 读系统提示词失败：%s" % e)
        return
    if not txt:
        print("[kv] boot: 系统提示词文件为空 -> 跳过预热")
        return
    ids = _sys_ids_from_text(txt)
    if ids is None:
        return
    if len(ids) < CTX.min_ctx:
        print("[kv] boot: 系统前缀仅 %d token（< min_ctx=%d）-> 不值得预热" % (len(ids), CTX.min_ctx))
        return
    if len(ids) + 2 > S.engine.ctx_limit:
        print("[kv] boot: 系统前缀 %d token 超出 SERVER_CTX %d -> 跳过"
              % (len(ids), S.engine.ctx_limit))
        return
    print("[kv] boot: 预热系统前缀 %d token（按 76 ms/token 约 %.1f 分钟；已有缓存则跳过）"
          % (len(ids), len(ids) * 0.076 / 60.0))
    _snap_sys_prefix(ids, why="boot")


# ---------------- 生成编排（流式/非流式共用） ----------------
def _kv_miss_diag(ids):
    """[KVREUSE] lookup 未命中时的分叉诊断：找最接近的快照并定位分叉 token。

    为什么必须有：不命中此前是**完全静默**的 —— 每轮白烧一遍 prefill（实测 3376 token
    ⇒ 首字 63s）却没有任何提示。诊断要一眼回答「分叉在第几个 token、两侧渲染成什么」，
    这正是修 KV 复用（如 chat_cli 曾丢弃 reasoning_content）时唯一有用的信息。
    """
    best_m, best_n = None, 0
    for m in CTX.snaps.values():
        n = 0
        for x, y in zip(m.prefix, ids):
            if x != y:
                break
            n += 1
        if n > best_n:
            best_m, best_n = m, n
    if best_m is None:
        print("[kv] lookup 未命中（尚无任何已注册快照）-> 全量 prefill")
        return
    print("[kv] lookup 未命中；最接近快照 n_ctx=%d，公共前缀=%d/%d -> 全量 prefill"
          % (best_m.n_ctx, best_n, len(ids)))
    try:
        lo = max(0, best_n - 8)
        print("[kv]   分叉处 期望=%r" % S.tokens.decode(best_m.prefix[lo:best_n + 12]))
        print("[kv]   分叉处 实际=%r" % S.tokens.decode(ids[lo:best_n + 12]))
    except Exception:                                # noqa: BLE001 诊断失败不影响生成
        pass


def generate_events(ids, params):
    """-> yield (channel, text)。channel ∈ {reasoning, content}。已做 think 切分与 stop 过滤。

    [KVREUSE] 命中已登记前缀时走 `GENS` 续跑（只 prefill 新增部分）；本轮结束再 SNAP 一份快照。
              复用链上任何一步失败都**降级为全量 prefill**（正确性优先，绝不静默出错）。
    """
    dec = S.tokens.stream_decoder()
    split = ThinkSplitter(thinking=params["thinking"])
    stop = StopFilter(params["stops"])
    out_ids = []
    n = 0

    # ---- 尝试前缀复用 ----
    kv = 0
    params["_kv_reused"] = 0
    if KV_REUSE and len(ids) >= CTX.min_ctx:
        try:
            hit, hn = CTX.lookup(ids)
        except Exception as e:                      # noqa: BLE001 索引异常不该影响生成
            print("[kv] lookup 失败：%s" % e)
            hit, hn = None, 0
        if hit is not None and 0 < hn < len(ids):
            tag = CTX.tag(hit.prefix)      # [KV-DISK] 键含引擎指纹（命名空间）
            try:
                # [KV-DISK] RESTOREF = RAM 未命中就读盘（重启后的唯一路径）；关掉落盘时退回纯 RAM 的 RESTORE
                got = S.engine.restoref(tag, hit.path) if KV_DISK else S.engine.restore(tag)
                if got == hit.n_ctx:
                    kv = got
                    params["_kv_reused"] = kv
                    CTX.touch(hit)
                    # 命中价值必须可见：本轮 prefill 从 len(ids) 降到 len(ids)-kv
                    print("[kv] HIT 前缀复用 %d/%d token（省掉 %.0f%% 的 prefill）"
                          % (kv, len(ids), 100.0 * kv / max(1, len(ids))))
                else:
                    print("[kv] RESTORE 长度不符 %d != %d -> 全量 prefill" % (got, hit.n_ctx))
            except EngineError as e:
                print("[kv] RESTORE 失败（%s）-> 全量 prefill" % e)
        else:
            _kv_miss_diag(ids)      # [KVREUSE] 不命中要说清原因，否则会被当成静默慢
            # [PRE-WARM] 不命中且固定前缀很长 ⇒ 先把它 prefill + 落盘，再回来续跑本轮。
            #   代价：本轮等于把前缀跑了一遍（本来也要跑），收益：第一轮之后必有快照。
            _sid = params.get("_sys_ids")
            if KV_SYS_SNAP and KV_WARM and _sid and len(_sid) >= KV_WARM_MIN and len(_sid) < len(ids):
                try:
                    _got = _snap_sys_prefix(_sid, why="预暖")
                    if _got:
                        kv = (S.engine.restoref(_got["tag"], CTX.path_for(_sid)) if KV_DISK
                              else S.engine.restore(_got["tag"]))
                        if kv != len(_sid):
                            print("[kv] PRE-WARM 恢复长度不符 %d != %d -> 回退全量" % (kv, len(_sid)))
                            kv = 0
                        else:
                            params["_kv_reused"] = kv
                            print("[kv] PRE-WARM 就绪：本轮只需再 prefill %d token（前缀 %d 已缓存）"
                                  % (len(ids) - kv, kv))
                except EngineError as e:
                    print("[kv] PRE-WARM 失败（%s）-> 全量 prefill" % e)
                    kv = 0

    # 冷启动代价必须可见：Agent 平台的 70K prompt 在这个口径下是 89 分钟，静默等待最要命
    if len(ids) - kv >= 4096:
        print("[kv] 冷启动：本轮需 prefill %d token，按 76 ms/token 约 %.1f 分钟"
              % (len(ids) - kv, (len(ids) - kv) * 0.076 / 60.0))

    gen = S.engine.generate(ids[kv:], params["max_new"], params["temp"], params["topk"],
                            params["top_p"], start=(kv or None))
    try:
        for tid in gen:
            out_ids.append(tid)
            n += 1
            delta = dec(out_ids)
            if not delta:
                continue
            for ch, text in split.feed(delta):
                if ch == "content":
                    text = stop.feed(text)
                    if not text:
                        continue
                yield ch, text
            if stop.hit:
                break
    finally:
        # 无论正常结束还是提前 break（stop 命中），都必须把引擎这一轮的输出**读完** ——
        #   否则残留的 TOK/END 会串进下一个请求（原实现 stop 命中时就有这个隐患）。
        try:
            for _ in gen:
                pass
        except Exception:                            # noqa: BLE001
            pass

    # ---- [THINK-GUARD 2026-09-28] 思考未闭合 -> 补 `</think>` 续跑 ----
    #   触发条件（全满足才做，且只做一次）：
    #     ① 请求开着思考（关思考时不存在"未闭合"这一说，切分器初始态就是 content）；
    #     ② 引擎是**自然收笔**（没被 stop 串截断；被上限截断的另说，那种情况下面还有正文余量问题）；
    #     ③ 切分器仍在 reasoning 态（整轮没见到 `</think>`）；
    #     ④ 确实生成了东西，且 KV 还装得下闭合符 + 后续正文。
    #   ⚠ 整个保护块包在宽 except 里：它只是**补救**，任何异常都必须退化成"原样返回已生成内容"，
    #     绝不能因为它而让一个本来能返回的请求变成 500。
    #   ⚠ 客户端显式给了 max_tokens 且已被它截断时**不补**（那是用户要的上限，不是模型收笔）；
    #     ARKION_THINK_GUARD_FORCE=1 可越过这条，用于确定性地验证保护逻辑本身。
    _tg_capped = int(params["max_new"]) > 0 and n >= int(params["max_new"])
    if (_THINK_GUARD and params["thinking"] and not stop.hit
            and split.state == "reasoning" and out_ids
            and (not _tg_capped or _THINK_GUARD_FORCE)
            and (len(ids) + len(out_ids) + 8) < S.engine.ctx_limit):
        try:
            _closer_ids = S.tokens.encode(THINK_CLOSER)
            _tag = "tg-" + _key(list(ids) + list(out_ids))
            _nb, _kvn = S.engine.snap(_tag)
            _got = S.engine.restore(_tag)
            # [THINK-GUARD-FIX 2026-09-28] 续跑起点必须用**引擎真实 KV 长度 _kvn**，
            #   不能拿 len(ids)+len(out_ids) 当起点。原因与 app.py 的 [SNAP-LEN] 同一件事：
            #   因「达到生成长度上限」收尾时，最后采样的那个 token 只发给了客户端、**还没被喂进
            #   引擎** ⇒ 引擎真实 KV 比它少 1。实测 `snap kvn=370 restore=370，期望 371`
            #   ⇒ 等式不等、保护被静默放弃（这正是"保护没生效"的真正原因）。
            #   修法：以 _kvn 为准，把 out_ids 里那些"发过但没进 KV"的尾巴裁掉，再从 _kvn 续跑。
            _m = _kvn - len(ids)
            if _got == _kvn and 0 <= _m <= len(out_ids):
                # 尾巴 = 已发给客户端、但还没进引擎 KV 的 token（通常是最后 1 个）。
                #   ⚠ 必须把它们**一并喂回去**（而不是丢掉）：它们是客户端已经看到的真实 token，
                #     不喂的话续跑时模型会把它们重新生成一遍 ⇒ 客户端看到重复文本
                #     （实测第一版就是这样：正文开头把思考的尾部又抄了一遍）。
                _feed = list(out_ids[_m:]) + _closer_ids
                _ilog("[think] EOS 时思考未闭合 -> 回喂 %d 个尾巴 token + 补 %r（%d token）后从 p=%d 续跑"
                      % (len(out_ids) - _m, THINK_CLOSER, len(_closer_ids), _kvn))
                # 把闭合符追加进 out_ids：dec() 会把它交给切分器完成状态翻转（`</think>` 本身
                # 被切分器当标记吃掉、不会作为正文外发），同时它也是 KV 里真实存在的位置。
                out_ids.extend(_closer_ids)
                gen2 = S.engine.generate(_feed, params["max_new"], params["temp"],
                                         params["topk"], params["top_p"], start=_kvn)
                try:
                    for tid in gen2:
                        out_ids.append(tid)
                        n += 1
                        delta = dec(out_ids)
                        if not delta:
                            continue
                        for ch, text in split.feed(delta):
                            if ch == "content":
                                text = stop.feed(text)
                                if not text:
                                    continue
                            yield ch, text
                        if stop.hit:
                            break
                finally:
                    try:
                        for _ in gen2:      # 必须把引擎这一轮读完，否则残留 TOK/END 串进下一个请求
                            pass
                    except Exception:       # noqa: BLE001
                        pass
            else:
                _ilog("[think] 保护续跑放弃：snap kvn=%s restore=%s，对应 out 长度 %s（实际 %d）"
                      % (_kvn, _got, _m, len(out_ids)))
            try:
                S.engine.drop(_tag)     # 保护用的临时快照必须立刻丢弃：
                                        #   否则它会占预算、并在 FIFO 淘汰时先挤掉有用的会话快照
            except Exception:           # noqa: BLE001
                pass
        except Exception as e:          # noqa: BLE001
            _ilog("[think] 保护续跑失败（原输出已保留）：%s" % e)
    elif params["thinking"] and split.state == "reasoning" and out_ids:
        # 只对「开着思考、却整轮没见到 </think>」这一种签名做诊断（正常轮次不该刷屏）。
        #   这正是故障特征（模型思考中途收笔）；必须能看见**是哪一项**挡住了保护，
        #   否则只能在黑箱里猜（曾因此误判过一次）。
        _ilog("[think] 保护未触发: guard=%s stop_hit=%s state=%s out=%d capped=%s force=%s room=%s"
              % (_THINK_GUARD, stop.hit, split.state, len(out_ids), _tg_capped,
                 _THINK_GUARD_FORCE, (len(ids) + len(out_ids) + 8) < S.engine.ctx_limit))

    for ch, text in split.flush():
        if text:
            yield ch, text
    tail = stop.flush()
    if tail:
        yield "content", tail
    params["_n_out"] = n
    # [FINISH-FIX 2026-09-28] 必须用**引擎实际用的上限**判定，不能用请求里的原值。
    #   请求省略 max_tokens / 传 0 表示「不限」，engine.generate 会把它补成「剩余 KV」；
    #   而原判定式 `n >= params["max_new"]` 在 max_new=0 时恒真 ⇒ 正常 EOS 收笔也被标成
    #   "length"。实测症状：用户问「你好」只生成 32 token 就自然收笔，却收到
    #   「达到生成长度上限被截断……请调大 SERVER_CTX 再重启服务」的提示，
    #   据此判断「上下文默认还是没有 128k」—— 而引擎日志里同一请求是
    #   `GEN ... max_new=130790`（128K 已生效），纯属误报。
    _cap = getattr(S.engine, "last_max_new", None) or params["max_new"]
    params["_finish"] = "length" if (int(_cap) > 0 and n >= int(_cap)) and not stop.hit else "stop"

    # ---- 保存快照点（供下一轮 / 下一会话复用）----
    #   ⚠ 顺序不可颠倒：(b) 必须在生成结束后**立刻**拍（那时引擎状态 = ids + out_ids）；
    #     而 (a) 会**改动引擎状态**（它要重跑一遍 sys 前缀的 prefill），所以必须放在最后。
    if KV_REUSE:
        # (b) 整段序列快照（多轮时若前缀恰好匹配可命中；不匹配则自然退化）
        if out_ids:
            full = list(ids) + list(out_ids)
            # [SNAP-LEN 2026-09-28] 因「达到生成长度上限」收尾时，**最后一个 token 还没被喂进引擎**：
            #   生成循环在 p=PMAX-1 处写完那一行的 KV 后就结束，最后采样的值只发给了客户端。
            #   ⇒ 引擎真实 KV 长度 = len(full) - 1。若照 len(full) 登记，下一轮 RESTORE 的长度校验
            #   必然不等（引擎日志 `[kv] (b) 整段快照：登记 full=250 kvn=249 一致=False`），
            #   复用被**安全拒绝**（回退全量 prefill）⇒ 白拍一份快照、白丢一次复用。
            #   故这里把该 token 从登记前缀里去掉，让登记长度与引擎真实状态严格一致。
            #   EOS 收笔不受影响：EOS 本身不进 out_ids，len(full) 本来就等于 KV 长度。
            if params["_finish"] == "length":
                full = full[:-1]
            if len(full) + params["max_new"] + 1 <= S.engine.ctx_limit:
                tag = CTX.tag(full)            # [KV-DISK] 键含引擎指纹（命名空间）
                try:
                    # [KV-DISK] 落盘（SNAPF）：进程内也留一份 RAM 副本（同进程命中免读盘）；
                    #   文件路径 = root/<键>.bin，与 CTX.submit 登记时一致。
                    if KV_DISK:
                        nb, kvn = S.engine.snapf(tag, CTX.path_for(full))
                    else:
                        nb, kvn = S.engine.snap(tag)
                    CTX.submit(full, nbytes=nb)
                    if KV_DISK:
                        CTX.save_index()          # 索引持久化：重启后前缀→文件映射仍在（否则白落盘）
                    params["_snap"] = {"tag": tag, "kv": kvn, "bytes": nb}
                    # [KVREUSE] 诊断：kvn 反映**引擎实际状态**，full 是**我们登记的前缀**。
                    #   两者必须相等，否则命中后会恢复到错误状态（静默的结果错误）。
                    #   ⚠ 仅长度相等还不够（曾出现「(a) 污染态 58+51」与「真态 63+46」同为 109
                    #      ⇒ 假阳性）。因此 (a) 必须排在本段之后，保证这里拍的是真态。
                    print("[kv] (b) 整段快照：登记 full=%d kvn=%d 一致=%s"
                          % (len(full), kvn, kvn == len(full)))
                except EngineError as e:
                    print("[kv] SNAP 失败：%s" % e)
        # (a) system 前缀快照：固定前缀，**跨会话**共享（Agent 平台的 30K 系统提示词就是它）。
        #     必须排在本段最后：prefill 会**覆盖引擎状态**，若放前面，(b) 会拍下这个污染态
        #     （曾导致复用恢复到错误历史）。现在走引擎的「只 prefill 不生成」（见 _snap_sys_prefix）。
        sys_ids = params.get("_sys_ids")
        if KV_SYS_SNAP and sys_ids and len(sys_ids) >= CTX.min_ctx \
                and len(sys_ids) + 2 <= S.engine.ctx_limit:
            try:
                got = _snap_sys_prefix(sys_ids, why="首请求")
                if got:
                    params["_snap_sys"] = got
            except EngineError as e:
                print("[kv] system SNAP 失败：%s" % e)


# ---------------- 路由 ----------------
@app.exception_handler(RequestValidationError)
async def _on_validation(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={
        "error": {"message": "请求体不合法: %s" % json.dumps(exc.errors(), ensure_ascii=False),
                  "type": "invalid_request_error", "code": "invalid_body", "param": None}})


@app.exception_handler(APIError)
async def _on_api_error(request: Request, exc: APIError):
    return JSONResponse(status_code=exc.status, content=exc.body())


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "engine": ENGINE_TAG, "model": MODEL_ID,
            "base_model": BASE_MODEL, "started": S._started,
            "ready_seconds": getattr(S._engine, "ready_seconds", None),
            "kv_reuse": KV_REUSE, "snaps": CTX.stats(),
            # [KV-DISK] 落盘缓存的开关/预算/目录 —— 排查"重启后为什么不命中"要看这三个
            "kv_disk": KV_DISK, "snap_disk_mb": _SNAP_DISK_MB, "snap_root": _SNAP_ROOT,
            # [PRE] 系统前缀快照（跨会话复用）的状态
            "kv_sys_snap": KV_SYS_SNAP, "kv_sys_scope": KV_SYS_SCOPE,
            "sys_prompt_file": SYS_PROMPT_FILE or None,
            # 引擎 KV 预留上限（行）：客户端据此推算历史字符上限（见 chat_cli --max-prompt-chars）
            "ctx_limit": getattr(S._engine, "ctx_limit", None)}


@app.get("/v1/models")
async def list_models():
    return JSONResponse(models_body())


@app.post("/arkion/v1/cancel")
async def cancel_generation():
    """取消当前正在进行的生成 —— **只终止这次生成**，引擎进程与服务都不退出。

    引擎在下一个位置边界停下并回 `END <已生成数>`，该请求的 SSE 正常收尾；
    此后引擎回到待命状态，可以立刻接下一个请求（会话态由下一个请求自行重置）。
    """
    try:
        ok = S.engine.cancel()
    except Exception as e:      # noqa: BLE001
        raise APIError("取消失败: %s" % e, status=500, err_type="server_error")
    return {"object": "arkion.cancel", "cancelled": bool(ok)}


@app.get("/arkion/v1/config")
async def get_config():
    """查看当前运行时配置（Arkion 扩展端点）。"""
    cfg = {
        "model": MODEL_ID,
        "base_model": BASE_MODEL,
        "engine": ENGINE_TAG,
        "kv_reuse": KV_REUSE,
        "kv_sys_snap": KV_SYS_SNAP,
        "default_think_template": _DEFAULT_THINK_ON,
        "snap_budget_mb": int(os.environ.get("ARKION_SNAP_BUDGET_MB", "3072")),
        "ctx_limit": getattr(S._engine, "ctx_limit", None) if S._started else None,
        "engine_exe": getattr(S._engine, "exe", None) if S._started else None,
        "default_max_new": DEFAULT_MAX_NEW if DEFAULT_MAX_NEW else "unlimited",
        "host": os.environ.get("ARKION_HOST", "127.0.0.1"),
        "port": int(os.environ.get("ARKION_PORT", "8471")),
    }
    return {"object": "arkion.config", "config": cfg}


def _headers(extra=None):
    h = {"X-Arkion-Engine": ENGINE_TAG, "X-Arkion-Base-Model": BASE_MODEL}
    h.update(extra or {})
    return h


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    """注意：**同步 def**（不是 async）—— FastAPI 会把它放进线程池，
    这样引擎启动（分钟级）与逐步生成都不会阻塞事件循环。"""
    params = validate(req)
    msgs = [m.model_dump() if hasattr(m, "model_dump") else m.dict() for m in req.messages]

    # [默认思考模板] 思考模式下且无 system message 时自动注入
    thinking_on = params.get("enable_thinking") is not False
    has_system = any((m.get("role") or "") == "system" for m in msgs)
    if _DEFAULT_THINK_ON and thinking_on and not has_system:
        msgs.insert(0, {"role": "system", "content": DEFAULT_THINK_TEMPLATE})

    try:
        ids = S.tokens.encode_messages(
            msgs, tools=req.tools, add_generation_prompt=True,
            reasoning_effort=params["reasoning_effort"],
            enable_thinking=params["enable_thinking"],
            preserve_thinking=params["preserve_thinking"])
    except PromptError as e:
        raise APIError(str(e), param="messages")

    # [KVREUSE] system 前缀的 token 序列：它是**固定前缀**（不随对话增长）⇒ 跨轮/跨会话共享，
    #   是最可靠的复用点（多轮整序列复用依赖「assistant 文本重编码 == 原 token 流」，而
    #   think 切分是有损文本操作 ⇒ 不可靠）。
    #   ⚠ 本模型的 chat 模板**不接受「只有 system」**（报 "No user query found"），
    #   所以用**差分法**取前缀：编码 [sys+user1] 与 [user1]，差值 k 即 system 段长度；
    #   再校验「后半段确实相同」才采用（不成立则放弃复用，退化为全量 prefill）。
    params["_sys_ids"] = None
    if KV_REUSE and KV_SYS_SNAP:      # 固定前缀推导的唯一用途就是 (a)；关掉 (a) 就不必算
        _sys = [m for m in msgs if (m.get("role") or "") == "system"]
        _usr = [m for m in msgs if (m.get("role") or "") == "user"]
        # 固定前缀 = 对话最前面**不随轮次变化**的那几条消息：
        #   sys（平台口径）⇒ **只取第一条 system**（静态提示词），动态记忆/用户输入留给后续位置
        #     ⇒ 任何新会话都能命中静态段。前提：静态段逐字节不变，记忆另起一条消息放在它之后。
        #   sys+user（默认）⇒ [全部 system, 首个 user]（沿用原行为：用户常把长材料放第一条里）。
        if KV_SYS_SCOPE == "sys":
            _head = _sys[:1] if _sys else (_usr[:1] if _usr else [])
        else:
            _head = (_sys + _usr[:1]) if _sys else (_usr[:1] if _usr else [])
        if _head:
            _kw = dict(tools=req.tools, add_generation_prompt=False,
                       reasoning_effort=params["reasoning_effort"],
                       enable_thinking=params["enable_thinking"],
                       preserve_thinking=params["preserve_thinking"])
            try:
                # 必须用本轮 ids **强制校验它确是前缀**（模板细节会随 tools/thinking 等选项变化，
                #   校验失败就放弃复用、退化为全量 prefill）。
                _a = list(S.tokens.encode_messages(_head, **_kw))
                if _a and len(_a) < len(ids) and list(ids[:len(_a)]) == _a:
                    params["_sys_ids"] = _a
                    # 构成必须可见：70K 的"固定前缀"到底在 system 里还是在首条 user 里，
                    # 决定了 scope=sys 能不能救它（在 user 里就只能靠"把稳定段放前面"的约定）。
                    _csys = sum(len(str(m.get("content") or "")) for m in _sys)
                    _cu1 = len(str((_usr[0].get("content") or ""))) if _usr else 0
                    print("[kv] 固定前缀(%s) = %d token（本轮共 %d）| 构成: system %d 条/%d 字符, 首条 user %d 字符"
                          % (KV_SYS_SCOPE, len(_a), len(ids), len(_sys), _csys, _cu1))
                else:
                    print("[kv] 固定前缀校验失败（%d vs 本轮 %d）-> 不复用" % (len(_a), len(ids)))
            except Exception as e:      # noqa: BLE001 推导失败不影响主流程
                print("[kv] 固定前缀推导失败：%s" % e)

    extra_h = {}
    if req.seed is not None:
        extra_h["X-Arkion-Seed-Ignored"] = "1"   # 引擎 SEED 是启动期 env，P0 不支持请求级
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]

    # 引擎尚未就绪 ⇒ 在返回响应体之前启动（启动耗时会体现在首个响应里，P1 改成后台预热）
    try:
        S.ensure_started()
    except EngineError as e:
        raise APIError("引擎不可用: %s" % e, status=503, err_type="server_error")

    n_prompt = len(ids)

    if not req.stream:
        reasoning, content = "", ""
        try:
            for ch, text in generate_events(ids, params):
                if ch == "reasoning":
                    reasoning += text
                else:
                    content += text
        except EngineError as e:
            raise APIError("生成失败: %s" % e, status=500, err_type="server_error")
        body = response_body(cid, MODEL_ID, content, reasoning, params["_finish"],
                             n_prompt, params.get("_n_out", 0))
        return JSONResponse(body, headers=_headers(extra_h))

    def sse():
        yield chunk(cid, MODEL_ID, {"role": "assistant"})
        reasoning, content = "", ""
        try:
            for ch, text in generate_events(ids, params):
                if ch == "reasoning":
                    reasoning += text
                    yield chunk(cid, MODEL_ID, {"reasoning_content": text})
                else:
                    content += text
                    yield chunk(cid, MODEL_ID, {"content": text})
        except EngineError as e:
            yield "data: " + json.dumps({"error": {"message": str(e), "type": "server_error"}},
                                        ensure_ascii=False) + "\n\n"
            yield DONE
            return
        n_out = params.get("_n_out", 0)
        yield chunk(cid, MODEL_ID, {}, finish_reason=params["_finish"])
        # stream_options.include_usage ⇒ 发送 usage 尾包
        include_usage = False
        if req.stream_options and isinstance(req.stream_options, dict):
            include_usage = bool(req.stream_options.get("include_usage"))
        if include_usage:
            yield usage_chunk(cid, MODEL_ID, n_prompt, n_out)
        yield DONE

    return StreamingResponse(sse(), media_type="text/event-stream", headers=_headers(extra_h))


# ---------------- 离线自测（Mock 引擎，不占端口、不需要 GPU） ----------------
def _selftest():
    os.environ["ARKION_MOCK"] = "1"
    from fastapi.testclient import TestClient

    c = TestClient(app)
    r = c.get("/v1/models")
    assert r.status_code == 200 and r.json()["data"][0]["id"] == MODEL_ID, r.text
    print("[1] /v1/models PASS  id=%s base=%s" % (MODEL_ID, BASE_MODEL))

    body = {"model": MODEL_ID, "messages": [{"role": "user", "content": "介绍一下你自己"}],
            "max_tokens": 64}
    r = c.post("/v1/chat/completions", json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    msg = j["choices"][0]["message"]
    print("[2] 非流式 PASS  finish=%s prompt=%d completion=%d"
          % (j["choices"][0]["finish_reason"], j["usage"]["prompt_tokens"],
             j["usage"]["completion_tokens"]))
    assert msg["reasoning_content"], "reasoning_content 未切出"
    assert msg["content"], "content 为空"
    print("    reasoning: %r" % msg["reasoning_content"][:40])
    print("    content  : %r" % msg["content"][:40])
    assert "</think>" not in msg["content"] and "</think>" not in msg["reasoning_content"]

    r = c.post("/v1/chat/completions", json=dict(body, stream=True))
    assert r.status_code == 200, r.text
    n_ch = n_rc = n_ct = 0
    seen_done = False
    for line in r.iter_lines():
        if not line:
            continue
        if line == "data: [DONE]":
            seen_done = True
            continue
        assert line.startswith("data: "), line[:80]
        d = json.loads(line[6:])["choices"][0]["delta"]
        if "reasoning_content" in d:
            n_rc += 1
        if "content" in d:
            n_ct += 1
        n_ch += 1
    assert seen_done and n_rc and n_ct, (seen_done, n_rc, n_ct)
    print("[3] 流式 PASS  chunks=%d reasoning块=%d content块=%d 且有 [DONE]" % (n_ch, n_rc, n_ct))

    for bad, why in [(dict(body, n=2), "n>1"),
                     (dict(body, logprobs=True), "logprobs"),
                     (dict(body, frequency_penalty=0.5), "penalty"),
                     (dict(body, reasoning_effort="highest"), "effort 值域"),
                     (dict(body, messages=[{"role": "user",
                                            "content": [{"type": "image", "image_url": {}}]}]),
                      "多模态")]:
        r = c.post("/v1/chat/completions", json=bad)
        assert r.status_code == 400 and "error" in r.json(), (why, r.status_code, r.text[:200])
        print("[4] 拒绝 %-12s PASS (%s)" % (why, r.json()["error"]["message"][:42]))

    # stop 序列
    r = c.post("/v1/chat/completions", json=dict(body, stop=["reasoning"]))
    j = r.json()
    print("[5] stop PASS  content=%r" % j["choices"][0]["message"]["content"][:40])
    assert "reasoning" not in j["choices"][0]["message"]["content"]

    print("\nserve 层自测全部 PASS")
    return 0


def _selftest_sdk(port=18471):
    """最关键的验证：**官方 openai SDK 直连本服务**（证明生态兼容，而不只是自测通过）。"""
    import threading
    import time as _t
    import urllib.request

    import uvicorn

    os.environ["ARKION_MOCK"] = "1"
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    url = "http://127.0.0.1:%d" % port
    for _ in range(200):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=0.5).read()
            break
        except Exception:
            _t.sleep(0.1)

    from openai import OpenAI
    cli = OpenAI(base_url=url + "/v1", api_key="arkion")

    ms = cli.models.list()
    assert ms.data[0].id == MODEL_ID, ms
    print("[1] SDK GET /v1/models PASS -> %s" % ms.data[0].id)

    r = cli.chat.completions.create(model=MODEL_ID, max_tokens=48,
                                    messages=[{"role": "user", "content": "介绍一下你自己"}])
    m = r.choices[0].message
    rc = (getattr(m, "model_extra", None) or {}).get("reasoning_content")
    assert m.content and rc, (m.content, rc)
    print("[2] SDK 非流式 PASS  finish=%s  usage=%s"
          % (r.choices[0].finish_reason, r.usage.total_tokens))
    print("    content          = %r" % m.content[:30])
    print("    reasoning_content= %r" % rc[:30])

    got_c = got_r = ""
    for ch in cli.chat.completions.create(model=MODEL_ID, max_tokens=48, stream=True,
                                          messages=[{"role": "user", "content": "介绍一下你自己"}]):
        d = ch.choices[0].delta
        got_c += (d.content or "")
        got_r += ((getattr(d, "model_extra", None) or {}).get("reasoning_content") or "")
    assert got_c and got_r
    print("[3] SDK 流式 PASS  正文 %d 字 / 思考 %d 字" % (len(got_c), len(got_r)))

    srv.should_exit = True
    _t.sleep(0.3)
    print("\n官方 openai SDK 直连 PASS —— 生态兼容性已验证")
    return 0


def main():
    import uvicorn
    host = os.environ.get("ARKION_HOST", "127.0.0.1")
    port = int(os.environ.get("ARKION_PORT", "8471"))
    mock = os.environ.get("ARKION_MOCK") == "1"
    print("[arkion-serve] %s -> http://%s:%d  (model=%s%s)"
          % (ENGINE_TAG, host, port, MODEL_ID, ", MOCK" if mock else ""))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    if "--selftest-sdk" in sys.argv:
        sys.exit(_selftest_sdk())
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    main()
