#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve/engine.py — Arkion-Q1 常驻引擎客户端（P0 消费层）

引擎侧协议（`SERVER=1`，引擎待实现；本文件先按此写好，配 MockEngine 联调）
--------------------------------------------------------------------------
请求（我们写引擎 stdin，一行一条）:
    GEN <n_ids> <id_0> ... <id_{n-1}> <max_new> <temp> <topk> <topp>
    PING
    QUIT
响应（引擎写 stdout，逐行）:
    READY                 启动完成，可以接请求
    TOK <id>              每生成一个 token 一行
    END <n_gen>           本次生成结束
    PONG                  对 PING 的应答
    ERR <msg>             出错
--------------------------------------------------------------------------
三条硬约定（引擎侧 P0 必做，否则接口是假的）:
  1. **请求级采样参数**：现在的 temp/topk/topp 是启动时读 env（src:5071-5074）⇒
     运行期改不了。协议里已带这三个参数，引擎必须按请求取值，否则
     per-request 的 temperature/top_p 会被静默忽略。
  2. **每请求重置会话态**：d_conv/d_S 回零 + qkv[li]=0 + pst.buf 清零 + MTP kv=0。
     初始态确实全零（src:4740-4742 `std::fill(...,0.f)`）⇒ 重置是 O(1) memcpy，
     且与已有的 snap_states/restore_states（src:4806-4821）复用同一套机制。
  3. **日志与协议分流**：引擎现有 130+ 处 printf 走 stdout，全改 stderr 风险大 ⇒
     P0 采用「协议行有固定前缀、客户端忽略其它行」。客户端只认
     READY / TOK / END / PONG / ERR，其余按日志透传（ARKION_LOG_PASSTHROUGH=1）。
"""
import os
import queue
import subprocess
import threading
import time

PROTO_PREFIXES = ("READY", "TOK ", "END ", "PONG", "ERR ")
READY_TOKENS = ("READY",)          # 引擎应打印 READY；兼容期也可配 "[chat] 自回归对话"
# 主构建（含 QSA 稀疏注意力 + SERVER 常驻协议）。可用 ARKION_EXE 覆盖（绝对路径）。
DEFAULT_EXE = os.path.join("bin", "arkion-q1.exe")
# [PATH-ENV 2026-10-01] 原为写死的 "E:/qw38"（本机 E 盘）。现在读 ARK_MODEL_DIR
#   （兼容 ARKION_MODEL_DIR）；都没设时才回落到旧值 —— 静默用 E:/qw38 会让"换了机器"
#   表现为后面一句与路径无关的加载失败。
DEFAULT_MODEL_DIR = (os.environ.get("ARK_MODEL_DIR")
                     or os.environ.get("ARKION_MODEL_DIR")
                     or "E:/qw38")
DEFAULT_GOLDEN = os.path.join("data", "golden")
# 引擎侧 KV 预留上限（行）：SERVER_CTX，默认 4096。prompt + max_new + 1 超过它引擎会回 ERR，
#   这里提前拦截以给出可读错误（引擎侧容量在启动期定格，无法按请求扩容）。
DEFAULT_CTX_LIMIT = 4096


class EngineError(RuntimeError):
    pass


class EngineProtocol:
    """协议编解码（纯函数，便于单测）。"""

    @staticmethod
    def encode_gen(ids, max_new, temp=1.0, topk=20, topp=0.95):
        if not ids:
            raise EngineError("prompt token 为空")
        head = ["GEN", str(len(ids))]
        head += [str(int(i)) for i in ids]
        head += [str(int(max_new)), "%.6f" % temp, str(int(topk)), "%.6f" % topp]
        return " ".join(head) + "\n"

    @staticmethod
    def parse(line):
        """-> (kind, payload)。kind ∈ {ready,tok,end,pong,ok,err,log}，payload 依 kind。"""
        s = line.strip()
        if s == "READY" or (s and all(s.startswith(p) for p in READY_TOKENS)):
            return "ready", None
        if s.startswith("TOK "):
            return "tok", int(s[4:])
        if s.startswith("END "):
            return "end", int(s[4:])
        if s == "PONG":
            return "pong", None
        if s.startswith("OK "):          # [KVREUSE] SNAP/RESTORE/DROP 的成功应答
            return "ok", s[3:]
        if s.startswith("ERR "):
            return "err", s[4:]
        return "log", s

    @staticmethod
    def encode_gens(start, ids, max_new, temp=1.0, topk=20, topp=0.95):
        """[KVREUSE] 续跑：前 start 个位置已在引擎 KV 里（须先 RESTORE 过同一快照）。"""
        if not ids:
            raise EngineError("prompt token 为空")
        head = ["GENS", str(int(start)), str(len(ids))]
        head += [str(int(i)) for i in ids]
        head += [str(int(max_new)), "%.6f" % temp, str(int(topk)), "%.6f" % topp]
        return " ".join(head) + "\n"


class EngineClient:
    """单并发常驻客户端：一次只有一个 GEN 在途，其余排队（P0 明确「单并发+排队」）。"""

    def __init__(self, exe=None, model_dir=None, golden_dir=None, env=None, env_drop=None,
                 startup_timeout=600.0, gen_timeout=1800.0, log_file=None):
        self.exe = exe or os.environ.get("ARKION_EXE") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DEFAULT_EXE)
        self.model_dir = model_dir or DEFAULT_MODEL_DIR
        self.golden_dir = golden_dir or DEFAULT_GOLDEN
        self.env_over = dict(env or {})
        # env_drop：**真正从子进程环境里删除**这些键。不能用「置空串」代替 —— 引擎侧用的是
        #   `getenv(k) != NULL` 语义，空串仍算「已设置」（曾因此让 PPL/PROMPT_FILE 泄漏进常驻模式）。
        self.env_drop = tuple(env_drop or ())
        # KV 预留上限：优先取 env_over 里的 SERVER_CTX，其次进程环境，最后默认值
        _ctx = self.env_over.get("SERVER_CTX") or os.environ.get("SERVER_CTX") or DEFAULT_CTX_LIMIT
        try:
            self.ctx_limit = int(_ctx)
        except (TypeError, ValueError):
            self.ctx_limit = DEFAULT_CTX_LIMIT
        self.startup_timeout = startup_timeout
        self.gen_timeout = gen_timeout
        self.log_file = log_file
        self.proc = None
        self._lock = threading.Lock()
        self._log_fh = None
        self.ready_seconds = None
        self._passthrough = os.environ.get("ARKION_LOG_PASSTHROUGH") == "1"

    # ---------- 生命周期 ----------
    def start(self):
        if self.proc and self.proc.poll() is None:
            return self
        env = dict(os.environ)
        for _k in self.env_drop:
            env.pop(_k, None)          # 必须真删（空串在引擎侧仍算「已设置」）
        env.update(self.env_over)
        env["SERVER"] = "1"
        cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # qw38-runtime
        if self.log_file:
            self._log_fh = open(self.log_file, "w", encoding="utf-8", errors="replace")
        t0 = time.time()
        self.proc = subprocess.Popen(
            [self.exe, self.model_dir, self.golden_dir],
            cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=(self._log_fh or subprocess.DEVNULL),
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        # 等 READY
        deadline = t0 + self.startup_timeout
        while True:
            if self.proc.poll() is not None:
                raise EngineError("引擎启动即退出，rc=%s（看 %s）" % (self.proc.returncode, self.log_file))
            if time.time() > deadline:
                self.kill()
                raise EngineError("引擎启动超时 %.0fs（未收到 READY）" % self.startup_timeout)
            line = self.proc.stdout.readline()
            if line == "":
                continue
            kind, _ = EngineProtocol.parse(line)
            if kind == "ready":
                break
            if kind == "log" and self._passthrough:
                print("[engine] " + line.rstrip())
        self.ready_seconds = time.time() - t0
        print("[engine] READY in %.1fs" % self.ready_seconds)
        return self

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def close(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.write("QUIT\n")
                    self.proc.stdin.flush()
                except Exception:
                    pass
            self.kill()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    def prefill(self, ids):
        """[PRE 2026-09-29] **只 prefill、不生成**：把引擎状态推进到 ids 结束处（KV 长度 = len(ids)）。

        为什么这正是跨会话命中的关键：Agent 平台的「系统提示词 + 记忆」动辄 30K token，
        冷启动按 76 ms/token 算是 **38 分钟**；而它每次开新会话都要重付一遍。
        有了 PRE 就能把「系统前缀」单独拍成一条快照 ⇒ 任何**以其为前缀**的请求都能命中。

        ⚠ 为什么不能走 `generate(ids, 0, ...)`：那里把 max_new<=0 解释为「按剩余 KV 补满」
        （见本文件 generate() 的注释），会**真的生成一大段**，拍下的快照 = sys + 生成内容 ⇒ 不可用
        （这正是原 (a) 路径长期失效、默认关闭的原因）。这里直接发字面 `max_new=0`：
        引擎侧 PMAX = NPRE + 0 ⇒ 位置循环跑完 prompt 即停、不采样，收尾打印 `END 0`。
        """
        if not ids:
            raise EngineError("prefill 的 ids 为空")
        if len(ids) + 2 > self.ctx_limit:
            raise EngineError("prefill 超出 SERVER_CTX：%d + 2 > %d（需调大后重启）"
                              % (len(ids), self.ctx_limit))
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                raise EngineError("引擎未运行")
            self._write(EngineProtocol.encode_gen(ids, 0, 1.0, 1, 1.0))
            ntok = 0
            deadline = time.time() + self.gen_timeout
            while True:
                if time.time() > deadline:
                    raise EngineError("prefill 超时（%.0fs）" % self.gen_timeout)
                line = self.proc.stdout.readline()
                if line == "":
                    if self.proc.poll() is not None:
                        raise EngineError("引擎在 prefill 中退出，rc=%s" % self.proc.returncode)
                    continue
                kind, payload = EngineProtocol.parse(line)
                if kind == "tok":
                    # 引擎的位置循环在最后一格仍会采样一次并把该 token 发出来（**没有**回灌进 KV）——
                    #   这就是 SNAP-LEN 那个 off-by-one 的来源（见 app.py 的 [SNAP-LEN] 注释）。
                    #   max_new=0 时它必然恰好出现在最后、且只有一个 ⇒ 忽略即可，KV 停在 len(ids)。
                    ntok += 1
                    if ntok > 1:
                        raise EngineError("prefill 产出了 %d 个 token —— 引擎 max_new=0 语义变了" % ntok)
                    continue
                if kind == "end":
                    # 引擎把那个"只采样未回灌"的 token 记进了 g_gen ⇒ max_new=0 时 END 报 1（不是 0）。
                    #   判据放宽到 <=1：多产就说明 max_new=0 的语义被改坏了。
                    if int(payload) > 1 or ntok > 1:
                        raise EngineError("prefill 产出了 %d 个 token（END %s）—— max_new=0 语义变了"
                                          % (ntok, payload))
                    return len(ids)
                if kind == "err":
                    raise EngineError("引擎报错: %s" % payload)
                if kind == "log" and self._passthrough:
                    print("[engine] " + line.rstrip())

    # ---------- 会话快照（KV 前缀复用）----------
    def _cmd(self, line):
        """发一条快照命令（SNAP/RESTORE/DROP），等 OK/ERR 应答，返回 OK 之后的 payload。"""
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                raise EngineError("引擎未运行")
            self._write(line + "\n")
            deadline = time.time() + self.gen_timeout
            while True:
                if time.time() > deadline:
                    raise EngineError("快照命令超时：%s" % line)
                l = self.proc.stdout.readline()
                if l == "":
                    if self.proc.poll() is not None:
                        raise EngineError("引擎在快照命令中退出，rc=%s" % self.proc.returncode)
                    continue
                kind, payload = EngineProtocol.parse(l)
                if kind == "ok":
                    return payload
                if kind == "err":
                    raise EngineError("引擎报错: %s" % payload)
                if kind == "log" and self._passthrough:
                    print("[engine] " + l.rstrip())

    def snap(self, tag):
        """保存当前会话态为 tag；返回 (bytes, n_ctx)。"""
        p = self._cmd("SNAP " + tag).split()          # "SNAP <tag> <bytes> <kv>"
        return int(p[-2]), int(p[-1])

    def restore(self, tag):
        """恢复到 tag 的会话态；返回恢复出的 KV 长度（n_ctx）。"""
        return int(self._cmd("RESTORE " + tag).split()[-1])

    def drop(self, tag):
        try:
            self._cmd("DROP " + tag)
            return True
        except EngineError:
            return False

    # ---------- 落盘快照（[KV-DISK 2026-09-29] 重启后仍可命中）----------
    #   与 snap/restore 的唯一差别是显式给文件路径：引擎保持无状态，磁盘预算/LRU/索引在
    #   context.py 这一层。失败（指纹/几何/格式不符）一律回 ERR ⇒ 调用方降级为全量 prefill。
    def snapf(self, tag, path):
        """保存会话态到文件；返回 (bytes, n_ctx)。"""
        p = self._cmd("SNAPF %s %s" % (tag, path)).split()    # "SNAPF <tag> <bytes> <kv>"
        return int(p[-2]), int(p[-1])

    def restoref(self, tag, path):
        """从文件恢复会话态（RAM 里没有时读盘）；返回恢复出的 KV 长度。"""
        return int(self._cmd("RESTOREF %s %s" % (tag, path)).split()[-1])

    def fingerprint(self):
        """[KV-DISK] 引擎的**快照指纹**（数值路径哈希：编译戳 + 数值开关白名单 + 数据根 + ARK_SNAP_FP）。

        用途：磁盘快照的命名空间。指纹不同 ⇒ 键不同 ⇒ 新快照不覆盖旧缓存（切回配置仍能命中），
        同时引擎在 RESTORE 时还会二次校验（指纹不符直接拒 ⇒ 上层降级全量 prefill，绝不静默错值）。
        """
        return self._cmd("FP").split()[-1]

    def cancel(self):
        """取消正在进行的生成：引擎在下一个位置边界退出并照常回 `END <已生成数>`。

        ⚠ 刻意**不持** self._lock —— 它正被 generate() 持有；CANCEL 是「另一条通道」，
        写 stdin 与读 stdout 互不干扰。取消后由 generate_events 的 finally 把残留输出 drain 掉。
        """
        p = self.proc
        if not p or p.poll() is not None:
            return False
        try:
            p.stdin.write("CANCEL\n")
            p.stdin.flush()
            return True
        except Exception as e:      # noqa: BLE001
            print("[engine] cancel 失败：%s" % e)
            return False

    # ---------- 请求 ----------
    def ping(self):
        with self._lock:
            self._write("PING\n")
            while True:
                line = self.proc.stdout.readline()
                if line == "":
                    raise EngineError("引擎管道关闭")
                kind, _ = EngineProtocol.parse(line)
                if kind == "pong":
                    return True
                if kind == "err":
                    raise EngineError("PING 失败")

    def generate(self, ids, max_new, temp=1.0, topk=20, topp=0.95, start=None):
        """同步生成器，逐个 yield token id。持有锁 ⇒ 天然单并发排队。

        start != None ⇒ 走 GENS 续跑（前 start 个位置已在引擎 KV 里；须先 restore() 过同一快照）。
        """
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                raise EngineError("引擎未运行")
            base = int(start or 0)
            # max_new <= 0 ⇒ **不限**：按剩余 KV 容量补满（留 1 个余量给引擎自己的边界检查）
            if int(max_new) <= 0:
                max_new = max(1, self.ctx_limit - base - len(ids) - 1)
            # [FINISH-FIX 2026-09-28] 记录**实际**用的上限，供服务层判定 finish_reason。
            #   调用方传 max_new=0（不限）时，服务层拿到的是 0 ⇒ 判定式 `n >= 0` 恒真
            #   ⇒ 每一轮都会被标成 finish="length"，哪怕引擎是正常 EOS 收笔。
            #   实测症状：用户单轮只生成 32 token 就收笔，却收到
            #   「达到生成长度上限被截断…请调大 SERVER_CTX」的误导提示（并据此以为上下文没到 128K）。
            self.last_max_new = int(max_new)
            # KV 预留上限校验：引擎容量在启动期定格，超限只会回 ERR ⇒ 这里给可读错误
            if base + len(ids) + int(max_new) + 1 > self.ctx_limit:
                raise EngineError(
                    "请求超出引擎 KV 预留：start %d + prompt %d + max_new %d + 1 > SERVER_CTX=%d"
                    "（需调大引擎 SERVER_CTX 后重启）" % (base, len(ids), max_new, self.ctx_limit))
            if start:
                self._write(EngineProtocol.encode_gens(base, ids, max_new, temp, topk, topp))
            else:
                self._write(EngineProtocol.encode_gen(ids, max_new, temp, topk, topp))
            deadline = time.time() + self.gen_timeout
            n = 0
            while True:
                if time.time() > deadline:
                    raise EngineError("生成超时（%.0fs）" % self.gen_timeout)
                line = self.proc.stdout.readline()
                if line == "":
                    if self.proc.poll() is not None:
                        raise EngineError("引擎在生成中退出，rc=%s" % self.proc.returncode)
                    continue
                kind, payload = EngineProtocol.parse(line)
                if kind == "tok":
                    n += 1
                    yield payload
                elif kind == "end":
                    self.last_n_gen = payload
                    return
                elif kind == "err":
                    raise EngineError("引擎报错: %s" % payload)
                elif kind == "log" and self._passthrough:
                    print("[engine] " + line.rstrip())

    def _write(self, s):
        try:
            self.proc.stdin.write(s)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise EngineError("写引擎 stdin 失败: %s" % e)


class MockEngine:
    """离线桩：不需要 GPU/磁盘，用于在 K3 校准占盘期间联调服务层与 SSE/think 切分。"""

    def __init__(self, encode, mock_text=None, delay=0.01):
        """encode: 文本->ids 的函数（由 Tokens 提供，保证 token 与真实 tokenizer 一致）。"""
        self.encode = encode
        self.delay = delay
        self.ready_seconds = 0.0
        # ★★★ [MOCK-SYNC 2026-10-01] 补齐真引擎（EngineClient）在 9/27-9/29 新增的那批接口。
        #   缺了它们的后果是实测过的：`python serve/app.py --selftest` 直接
        #   `AttributeError: 'MockEngine' object has no attribute 'prefill'`
        #   （app.py 的 _snap_sys_prefix 会调 S.engine.prefill）⇒ 服务层的离线自检
        #   自 KV 复用/快照功能上线起就是坏的，那条默认请求路径一直**零覆盖**。
        self.exe = "<mock>"
        self.ctx_limit = int(os.environ.get("SERVER_CTX", "4096"))
        self.last_max_new = 0
        self._kvn = 0            # 模拟的 KV 长度（由 prefill/generate 维护）
        self._snaps = {}
        self.mock_text = mock_text if mock_text is not None else (
            "用户问的是本地推理引擎的消费层。先确认三个前提：一是引擎已常驻，"
            "二是采样参数请求级生效，三是思考段要能被切出来。\n"
            "</think>\n\n"
            "Arkion-Q1 服务层已跑通：mock 引擎返回的这段话会被切成 "
            "reasoning_content 与 content 两段，并可经 SSE 逐块下发。")

    def start(self):
        return self

    def kill(self):
        pass

    def close(self):
        pass

    def ping(self):
        return True

    # ---- [MOCK-SYNC] 与 EngineClient 对齐的 KV/快照接口（语义按"长度"记账，够 app.py 校验）----
    def prefill(self, ids):
        """只跑 prompt、不生成（真引擎发字面 max_new=0）。"""
        self._kvn = len(ids)
        return self._kvn

    def snap(self, tag):
        self._snaps[tag] = self._kvn
        return (self._kvn * 4096, self._kvn)      # -> (nbytes, kvn)，app.py 用 kvn 校验长度

    def snapf(self, tag, path):
        return self.snap(tag)

    def restore(self, tag):
        return self._snaps.get(tag, 0)

    def restoref(self, tag, path):
        return self.restore(tag)

    def drop(self, tag):
        self._snaps.pop(tag, None)

    def cancel(self):
        return True

    def generate(self, ids, max_new, temp=1.0, topk=20, topp=0.95, start=None):
        base = int(start or 0)
        n = 0
        for tid in self.encode(self.mock_text):
            if self.delay:
                time.sleep(self.delay)
            n += 1
            yield tid
        self._kvn = base + len(ids) + n
        self.last_max_new = int(max_new)


# 必须从子进程环境里**真正删除**的启动期开关（与常驻协议语义冲突）：
#   PPL / PROMPT_FILE 会让引擎走教师强制而非自回归；
#   CHAT / GEN_MAX / TOPK / QTEMP / TOPP 由 GEN 行按请求提供（引擎侧 server_loop 覆盖，
#   且 g_chat 由 SERVER=1 自动置起）。
#   ⚠ 旧版把 K4_ALL / PLE_GPU / RELEASE_CODE / WIDE_GEMV 也列进来了 —— 它们是**生产必需**，
#   删掉会让引擎退化成慢路径（甚至行为异常），已剔除。
ENGINE_ENV_DROP = ("PPL", "PROMPT_FILE", "CHAT", "GEN_MAX", "TOPK", "QTEMP", "TOPP",
                   "reasoning_effort")


def make_engine(tokens=None, use_mock=None, **kw):
    """工厂：ARKION_MOCK=1 或 use_mock=True 时返回 MockEngine。"""
    if use_mock is None:
        use_mock = os.environ.get("ARKION_MOCK") == "1"
    if use_mock:
        if tokens is None:
            raise EngineError("MockEngine 需要 tokens（用于把 mock 文本编码成 token id）")
        return MockEngine(tokens.encode)
    env = dict(kw.pop("env", {}) or {})
    drop = tuple(ENGINE_ENV_DROP) + tuple(kw.pop("env_drop", ()) or ())
    # 引擎侧 stderr 默认被 DEVNULL 丢弃 ⇒ 显存报告（[mem] after upload / [ZONE] vram）、
    #   [KVTIER]、[chat] EOS 等全部看不到，排查容量/IO 问题时等于瞎。设 ARKION_ENGINE_LOG
    #   即可落盘（路径相对 qw38-runtime，即引擎 cwd）。
    kw.setdefault("log_file", os.environ.get("ARKION_ENGINE_LOG"))
    return EngineClient(env=env, env_drop=drop, **kw)


if __name__ == "__main__":
    # 单测：协议编解码 + Mock 生成
    line = EngineProtocol.encode_gen([1, 2, 3], 8, 0.7, 20, 0.9)
    assert line == "GEN 3 1 2 3 8 0.700000 20 0.900000\n", repr(line)
    assert EngineProtocol.parse("TOK 42") == ("tok", 42)
    assert EngineProtocol.parse("END 8") == ("end", 8)
    assert EngineProtocol.parse("[load] pack 索引")[0] == "log"
    m = MockEngine(encode=lambda s: list(range(10)), delay=0)
    got = list(m.generate([1], 5))
    assert got == list(range(10)), got
    print("engine.py 自测 PASS")
