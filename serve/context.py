#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve/context.py — Arkion-Q1 上下文管理器（自研 KV/会话快照的「策略与索引」层）

★ 必须先把责任边界讲清（否则会做成一个做不到事的插件）:
------------------------------------------------------------------
本引擎的会话状态是 **VRAM 里的 device 张量**（源码 4748/4789/4758 定义），Python 侧
摸不到、也搬不动。所以：

  · **引擎侧（唯一能做搬运的地方）** 必须暴露这几个 op（随 SERVER=1 一起实现）：
        SNAP   <slot> <path>    把当前会话状态整块写出（device → host → 文件）
        RESTORE <slot> <path>   读回并装载（文件 → host → device）
        DROP   <slot>           丢弃（等价于「回零」）
    重置本来就是 O(1)：状态初值**全零**（src:4740-4742 `std::fill(...,0.f)`），
    且已有 snap_states/restore_states（src:4806-4821）现成的 D2D 机制可复用。
  · **本文件（策略层）** 负责：快照点登记、**最长前缀匹配**、磁盘布局、LRU 淘汰、
    预算控制。全部可在 Mock 下测通，等引擎就绪只需把 StateBackend 换成真引擎实现。

★ 为什么「最长前缀」就是 KV 复用的全部（这是本引擎的特殊性）:
------------------------------------------------------------------
  48 层里只有 12 层 QSA（li%4==3）是**追加式 KV**（可用计数器截断）；
  另外 36 层是 **GDN 原地递推态**（conv 5.9MB + S 151MB + PLE 0.37MB）——
  没有逐位置可切分的数组形态，**不能任意截断**。
  ⇒ 经典 prefix cache（任意位置 hash 命中）对 36 层不成立；
  ⇒ 命中只能是「**整快照命中**」，即候选快照的 token 序列必须是新请求的前缀；
  ⇒ 所以本引擎的 cache 命中判据是「**最长前缀等于某个已注册快照点**」。

★ 快照点该设在哪（由 agent 负载模式决定，不是随便设）:
------------------------------------------------------------------
  agent 的典型请求 = 固定 system ＋ 单调增长的历史 ＋ 追加的工具结果。
  若把快照点设在**轮边界**（每条 assistant/tool 消息之后），则下一轮请求的前缀
  恰好等于上一轮的快照点 ⇒ **命中率接近 100%，每轮只需 prefill 新增部分**。
  更强的一点：**system 末尾也放一个快照点**，它只由固定的 system 决定 ⇒
  **可跨会话共享**（这就是 vLLM prefix cache 的等价物，对「长 system + 多会话」收益巨大）。

★ 定量依据（为什么这条路必须走）:
------------------------------------------------------------------
  prefill 实测只有 **10.5–11.8 tok/s**（teacher-forced 512 位置 / 48.6s，见 `_m115_n1.log`）。
  ⇒ 3000 token 的 system prompt 需 ~255 s；每轮重算 = 分钟级 ⇒ 无状态重算复用不可行。
  而快照代价：D2D 155MB ≈ 2.7 ms（§101 实测）+ D2H 155MB@22GB/s ≈ 7 ms
  ⇒ **RAM 中快照/恢复 ≈10 ms，落盘 ≈45 ms**，比重算便宜 4~5 个数量级。
"""
import hashlib
import json
import os
import time

# ---- 引擎常量（与 src/helm_qw38_gpu2.cu:35-43 对齐，改动时必须同步）----
NLAYER, HDIM, HC, PLE_STATE = 48, 2560, 4, 9
NHV, DK, NKV, DH = 48, 128, 2, 256
NQSA = 12                                   # li % 4 == 3 的层数（源码 4772）

# ---- 状态尺寸（字节）----
CONV_BYTES = NLAYER * 3 * 4 * HDIM * 4            # d_conv  5.90 MB
S_BYTES = NLAYER * NHV * DK * DK * 4              # d_S   151.0 MB
PST_BYTES = PLE_STATE * HC * HDIM * 4             # pst     0.37 MB
RECURRENT_BYTES = CONV_BYTES + S_BYTES + PST_BYTES   # ≈ 157.3 MB（与契约 §101「155 MB」吻合）
# [Q2-SNAP 2026-09-28] 口径修正为引擎**实际序列化**的内容：
#   2-bit KV 主存（K/V 各 384 B/行）+ 块键 d_ik（每 4 行 1 块 × 128 值 × 4 B = 128 B/行）
#   ⇒ 每位置每层 896 B，12 层合计 10,752 B/位置。
#   旧值 55,296 是 **bf16** 口径（4 倍偏大）：128K 时会把估算抬到 7.4 GB，而实际约 1.4 GB
#   ⇒ 服务端索引会过早淘汰快照、/v1/models 的披露也不实。
QSA_KV_PER_CTX = NQSA * (2 * 384 + 128)                # 10,752 B / 位置（d_Kq2+d_Vq2+d_ik）
# MTP 的 KV **不计入**：引擎 do_snap 不序列化 MTP KV（生产 K4_MTP=0）。旧代码把它算进来是虚增，
#   故置 0（保留常量名以免调用点改动）。
MTP_KV_PER_CTX = 0                                     # 4,096 B / 位置（旧 bf16 口径，已不适用）

MAGIC = "AKS1"
INDEX_NAME = "index.json"


def estimate_snapshot_bytes(n_ctx):
    """一次完整会话快照的大小（递推态 + QSA KV + MTP KV）——用于预算与 /v1/models 披露。"""
    return RECURRENT_BYTES + n_ctx * QSA_KV_PER_CTX + n_ctx * MTP_KV_PER_CTX


def _key(ids, stamp=""):
    """前缀键：sha1(stamp + 前缀 token)。用 sha1 而非 tuple（省内存，且索引文件里可读）。

    [KV-DISK 2026-09-29] `stamp` = 引擎报的快照指纹（编译戳 + 数值开关 + 数据根）。
    把它并进键 = **磁盘命名空间**：换开关/重编译后新快照落在新键上，**旧缓存文件不被覆盖**，
    所以"切回原配置立刻又能命中"。而正确性另有引擎侧二次校验（指纹不符直接拒）。
    """
    h = hashlib.sha1()
    if stamp:
        h.update(stamp.encode())
        h.update(b"|")
    h.update(b",".join(str(int(i)).encode() for i in ids))
    return h.hexdigest()[:24]


def snap_path(root, ids, stamp=""):
    """落盘快照的文件路径（与引擎 SNAPF/RESTOREF 的 <path> 参数同一约定）。

    [KV-DISK 2026-09-29] 布局 `root/<key>.bin`（单文件，与引擎侧一段连续容器一一对应）。
    引擎自己不做任何磁盘管理（无预算/无 LRU/无索引）⇒ 全部策略在这一层，便于 A/B 与人工清理。
    """
    return os.path.join(root, _key(ids, stamp) + ".bin")


class SessionMeta:
    __slots__ = ("sid", "prefix", "n_ctx", "path", "bytes", "created", "used", "hits", "stamp")

    def __init__(self, sid, prefix, n_ctx, path, nbytes, created=None, used=None, hits=0, stamp=""):
        self.sid = sid
        self.prefix = list(prefix)      # 该快照对应的完整 token 前缀
        self.n_ctx = n_ctx
        self.path = path
        self.bytes = nbytes
        self.created = created or time.time()
        self.used = used or self.created
        self.hits = hits
        self.stamp = stamp              # [KV-DISK] 引擎快照指纹（命名空间），见 _key()

    def to_json(self):
        return {"sid": self.sid, "prefix": self.prefix, "n_ctx": self.n_ctx, "path": self.path,
                "bytes": self.bytes, "created": self.created, "used": self.used, "hits": self.hits,
                "stamp": self.stamp}

    @staticmethod
    def from_json(d):
        return SessionMeta(d["sid"], d["prefix"], d["n_ctx"], d["path"], d["bytes"],
                           d.get("created"), d.get("used"), d.get("hits", 0), d.get("stamp", ""))

    def __repr__(self):
        return ("<Snap %s n_ctx=%d %.1fMB hits=%d>" %
                (self.sid, self.n_ctx, self.bytes / 1e6, self.hits))


class StateBackend:
    """引擎状态搬运接口。真实现 = EngineClient 的 SNAP/RESTORE op；测试用 FakeBackend。"""

    def snapshot(self, path):
        raise NotImplementedError

    def restore(self, path):
        raise NotImplementedError


class FakeBackend(StateBackend):
    """离线桩：写一个固定大小的假状态块（只用来测策略层，不碰 VRAM/磁盘真实格式）。"""

    def __init__(self, nbytes=1024, log=None):
        self.nbytes = nbytes
        self.log = log if log is not None else []
        self.snapped_n_ctx = None

    def snapshot(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\0" * self.nbytes)
        self.log.append(("SNAP", path))

    def restore(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.log.append(("RESTORE", path))


class ContextManager:
    """会话快照的登记 / 最长前缀匹配 / 磁盘预算与 LRU 淘汰。

    budget_bytes: 磁盘快照总预算（默认 8 GiB）。超限按 LRU 淘汰。
    backend:      StateBackend；为 None 时只做索引（submit() 不落字节）——
                  这样 mock 测试与真引擎共用同一套策略代码。
    """

    def __init__(self, root, budget_bytes=8 << 30, backend=None, min_ctx=16, stamp=""):
        self.root = os.path.abspath(root)
        self.budget = int(budget_bytes)
        self.backend = backend
        self.min_ctx = int(min_ctx)         # 太短的前缀不值得存（快照成本 > 重算成本）
        self.stamp = stamp                  # [KV-DISK] 引擎快照指纹（命名空间，见 _key）
        self.snaps = {}                     # sid -> SessionMeta
        self.used_bytes = 0
        os.makedirs(self.root, exist_ok=True)

    def set_stamp(self, stamp):
        """[KV-DISK] 绑定引擎指纹（引擎起来后由 app.py 调一次）。"""
        self.stamp = stamp or ""
        return self.stamp

    def tag(self, ids):
        """本命名空间下该前缀的键（= 引擎 SNAPF 的 tag / 文件名）。"""
        return _key(ids, self.stamp)

    def exact(self, ids):
        """本命名空间下**恰好等于** ids 的条目（用于"这个前缀是否已拍过快照"的幂等判断）。

        [PRE 2026-09-29] 启动预热/首次请求靠它避免重复 prefill：重启后索引已恢复 ⇒ 直接跳过。
        """
        if len(ids) < self.min_ctx:
            return None
        m = self.snaps.get(self.tag(ids))
        return m if (m is not None and len(m.prefix) == len(ids)) else None

    def path_for(self, ids):
        """本命名空间下该前缀的落盘路径（= 引擎 SNAPF 的 <path>）。"""
        return snap_path(self.root, ids, self.stamp)

    # ---------- 命中判定（核心）----------
    def lookup(self, ids, margin=0):
        """返回 (SessionMeta | None, 命中前缀长度)。

        判据：候选快照的 prefix 必须是 ids 的前缀；取最长者。
        要求 len(prefix) <= len(ids) - margin，margin 留给「至少要新增几个 token」的场景。
        [KV-DISK] 只认**本指纹命名空间**的条目：其它配置/其它构建留下的快照仍留在索引与磁盘上
        （切回去立刻可命中），但绝不参与当前配置的匹配 —— 数值正确性优先。
        """
        best, best_n = None, 0
        limit = len(ids) - max(0, margin)
        for m in self.snaps.values():
            if m.stamp != self.stamp:      # 命名空间不匹配 ⇒ 跳过（文件不删，留给切回原配置）
                continue
            n = len(m.prefix)
            if n == 0 or n > limit or n <= best_n:
                continue
            if ids[:n] == m.prefix:
                best, best_n = m, n
        return best, best_n

    # ---------- 登记 ----------
    def submit(self, ids, sid=None, prefix_len=None, tag="turn", nbytes=None):
        """为「ids 的某个前缀」登记一个快照点。

        ids        : 该快照点对应的**完整 token 前缀**（写进 prefix，用于匹配）
        prefix_len : 只登记 ids 的前 prefix_len 个（默认全部）；用于「只快照已提交前缀」
        nbytes     : 引擎实测的快照字节数（[KV-DISK] 落盘后用它记账；None ⇒ 用估算公式）
        返回 SessionMeta。
        """
        ids = [int(i) for i in ids]
        if prefix_len is not None:
            ids = ids[:prefix_len]
        if len(ids) < self.min_ctx:
            return None
        key = self.tag(ids)                              # [KV-DISK] 键含指纹 ⇒ 天然按命名空间隔离
        if key in self.snaps and self.snaps[key].n_ctx == len(ids):
            self.touch(key)
            return self.snaps[key]
        sid = sid or ("%s-%s" % (tag, key))
        path = self.path_for(ids)                        # 与引擎 SNAPF 的落盘路径同一约定
        nbytes = int(nbytes) if nbytes else estimate_snapshot_bytes(len(ids))
        m = SessionMeta(sid, ids, len(ids), path, nbytes, stamp=self.stamp)
        if self.backend is not None:
            self.backend.snapshot(path)
            real = os.path.getsize(path) if os.path.exists(path) else nbytes
            m.bytes = real
        self.snaps[key] = m
        self.used_bytes += m.bytes
        self._evict_if_needed(protect=key)
        return m

    def touch(self, key_or_meta):
        key = key_or_meta if isinstance(key_or_meta, str) else _key(key_or_meta.prefix, key_or_meta.stamp)
        m = self.snaps.get(key)
        if m:
            m.used = time.time()
            m.hits += 1
        return m

    def restore(self, meta):
        """把快照装回引擎（真引擎下这会是一次 H2D；mock 下只记 log）。"""
        if meta is None:
            return False
        if self.backend is not None:
            self.backend.restore(meta.path)
        self.touch(meta)
        return True

    def drop(self, meta):
        if meta is None:
            return
        key = _key(meta.prefix, meta.stamp)
        if self.snaps.pop(key, None):
            self.used_bytes -= meta.bytes
            try:
                if os.path.exists(meta.path):
                    os.remove(meta.path)
            except OSError:
                pass

    # ---------- 预算与淘汰 ----------
    def _evict_if_needed(self, protect=None):
        if self.used_bytes <= self.budget:
            return []
        victims = sorted(self.snaps.items(), key=lambda kv: kv[1].used)
        evicted = []
        for key, m in victims:
            if self.used_bytes <= self.budget:
                break
            if key == protect:
                continue
            self.drop(m)
            evicted.append(m)
        return evicted

    def stats(self):
        return {"snaps": len(self.snaps), "used_bytes": self.used_bytes,
                "budget_bytes": self.budget, "hits": sum(m.hits for m in self.snaps.values()),
                # [KV-DISK] 当前指纹命名空间下的条目数（其余条目仍留在磁盘，切回原配置即可命中）
                "current_stamp_snaps": sum(1 for m in self.snaps.values() if m.stamp == self.stamp),
                "stamp": self.stamp}

    # ---------- 持久化 ----------
    def save_index(self):
        p = os.path.join(self.root, INDEX_NAME)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"magic": MAGIC, "snaps": [m.to_json() for m in self.snaps.values()]}, f)
        os.replace(tmp, p)
        return p

    def load_index(self):
        p = os.path.join(self.root, INDEX_NAME)
        if not os.path.exists(p):
            return 0
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("magic") != MAGIC:
            raise ValueError("快照索引版本不匹配: %s" % d.get("magic"))
        self.snaps, self.used_bytes = {}, 0
        for j in d.get("snaps", []):
            m = SessionMeta.from_json(j)
            if os.path.exists(m.path):
                self.snaps[_key(m.prefix, m.stamp)] = m
                self.used_bytes += m.bytes
        return len(self.snaps)


# ================= 自测：模拟 agent 的三轮对话 =================
def _selftest():
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="aks_")
    try:
        be = FakeBackend(nbytes=256)
        cm = ContextManager(root, budget_bytes=1 << 20, backend=be, min_ctx=8)

        SYS = list(range(100, 130))                 # 固定 system（30 token）
        print("[1] 状态尺寸: 递推态 %.1f MB | 每位置 QSA %.1f KB | ctx4096 快照 %.0f MB"
              % (RECURRENT_BYTES / 1e6, QSA_KV_PER_CTX / 1024,
                 estimate_snapshot_bytes(4096) / 1e6))

        # 轮次 1：sys + u1
        # ★ 快照点必须**显式登记**：插件不知道语义边界在哪，由调用者告诉它。
        #   「system 末尾」这一刻的状态只由 SYS 决定 ⇒ 登记后可跨会话共享。
        cm.submit(SYS, tag="sysonly")
        r1 = SYS + [1, 2, 3, 4]
        cm.submit(r1, tag="sys")
        a1 = r1 + [900, 901]                        # assistant 输出
        cm.submit(a1, tag="turn1")
        # 轮次 2：sys + u1 + a1 + tool + u2
        r2 = a1 + [50, 51, 52]
        m, n = cm.lookup(r2)
        assert m is not None and n == len(a1), (m, n, len(a1))
        print("[2] 轮2 命中: %r 命中前缀=%d/%d (应=%d == 上次快照长度) PASS" % (m, n, len(r2), len(a1)))
        cm.restore(m)

        # 新会话共享 system 快照
        r3 = SYS + [7, 8, 9]
        m3, n3 = cm.lookup(r3)
        assert n3 == len(SYS), (n3, len(SYS))
        print("[3] 新会话共享 system 快照: 命中 %d/%d PASS" % (n3, len(r3)))

        # 无命中（完全不同）
        m4, n4 = cm.lookup([5, 5, 5, 5, 5, 5, 5, 5, 5, 5])
        assert m4 is None and n4 == 0
        print("[4] 无关前缀: 未命中 PASS")

        # 太短不登记
        assert cm.submit([1, 2, 3]) is None
        print("[5] 短于 min_ctx 不登记 PASS")

        # 预算淘汰（把预算压到 1 个快照大小）
        cm.budget = int(cm.snaps[_key(a1)].bytes * 1.2)
        before = len(cm.snaps)
        cm.submit(SYS + [200, 201, 202, 203], tag="big")
        after = len(cm.snaps)
        assert after <= before + 1, (before, after)
        print("[6] 预算淘汰: %d -> %d 快照 (used=%.0f B / budget=%d) PASS"
              % (before, after, cm.used_bytes, cm.budget))

        # 索引持久化（注意：上一轮淘汰已删掉 a1 的快照，这里重建一条做验证）
        cm.budget = 1 << 30
        cm.submit(a1, tag="turn1")
        p = cm.save_index()
        cm2 = ContextManager(root, backend=be, min_ctx=8)
        n = cm2.load_index()
        assert n == len(cm.snaps), (n, len(cm.snaps))
        m5, n5 = cm2.lookup(r2)
        assert n5 == len(a1)
        print("[7] 索引持久化: %s 载入 %d 条, 复查命中=%d PASS" % (os.path.basename(p), n, n5))

        # 后端调用轨迹（证明「策略层不碰字节」）
        kinds = [k for k, _ in be.log]
        print("[8] 后端调用: %s -> %d 次 PASS" % (set(kinds), len(be.log)))
        print("    stats=%s" % cm.stats())
        print("\ncontext.py 自测全部 PASS")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


def _sim(rounds=10, sys_len=1200, user_len=60, asst_len=400, tool_len=300,
         prefill_tps=11.0, decode_tps=10.5, verbose=True):
    """agent 多轮对话的 prefill 成本模拟（用实测速率，不需要引擎）。

    口径（重要）：assistant 的输出是引擎自己 decode 出来的，它已经落在会话状态里 ⇒
    **下一轮需要 prefill 的只有「新增的 user/tool 消息」**，不是整个 prompt。
    这正是轮边界快照能拿到的收益。
    """
    ms_pf = 1000.0 / prefill_tps
    ms_dc = 1000.0 / decode_tps
    rows, no_pf, wc_pf, dc_total = [], 0, 0, 0
    for i in range(1, rounds + 1):
        prompt_len = sys_len + (user_len + asst_len + tool_len) * (i - 1) + user_len
        delta = prompt_len - (sys_len + (user_len + asst_len + tool_len) * (i - 2) + user_len) if i > 1 \
            else prompt_len
        # 有快照：第 1 轮 prefill 全量；之后只 prefill 上一轮产生的新消息（tool + user）
        add = prompt_len if i == 1 else (tool_len + user_len)
        gen = asst_len
        no_pf += prompt_len
        wc_pf += add
        dc_total += gen
        rows.append((i, prompt_len, add, gen))

    no_total = no_pf * ms_pf + dc_total * ms_dc
    wc_total = wc_pf * ms_pf + dc_total * ms_dc
    if verbose:
        print("=== agent 多轮：%.0f token system, %d 轮, 每轮 user %d / asst %d / tool %d ==="
              % (sys_len, rounds, user_len, asst_len, tool_len))
        print("  prefill %.1f tok/s | decode %.1f tok/s（实测口径）" % (prefill_tps, decode_tps))
        print("  轮 | prompt长度 | 有快照的新增 | 生成")
        for r in rows:
            print("  %2d | %10d | %12d | %4d" % r)
        print("  ---------------------------------------------------------------")
        print("  无 cache: prefill %6d 位置 = %6.1f s  + decode %5.1f s  => 总 %6.1f s (%.1f 分)"
              % (no_pf, no_pf * ms_pf / 1000, dc_total * ms_dc / 1000, no_total / 1000, no_total / 60000))
        print("  有 cache: prefill %6d 位置 = %6.1f s  + decode %5.1f s  => 总 %6.1f s (%.1f 分)"
              % (wc_pf, wc_pf * ms_pf / 1000, dc_total * ms_dc / 1000, wc_total / 1000, wc_total / 60000))
        print("  => prefill 位置省 %.1f×，端到端快 %.2f×" % (no_pf / wc_pf, no_total / wc_total))
        print("  快照开销：每轮 1 次（%.0f MB @ ctx4096），%.0f ms/次(落盘) 或 %.0f ms/次(RAM) => %d 轮共 %.1f~%.1f s"
              % (estimate_snapshot_bytes(4096) / 1e6, 45, 10, rounds, rounds * 0.045, rounds * 0.010))
    return {"no_cache_s": no_total / 1000, "with_cache_s": wc_total / 1000,
            "speedup": no_total / wc_total, "prefill_saved_x": no_pf / wc_pf}


if __name__ == "__main__":
    import sys
    if "--sim" in sys.argv:
        _sim()
        print("\n-- 长 system 多会话（共享 system 末尾快照）--")
        _sim(rounds=4, sys_len=3000)
        sys.exit(0)
    sys.exit(_selftest())
