# -*- coding: utf-8 -*-
"""serve/locate_model.py — 找到并**校验**模型目录（不猜、不自动采纳）。

背景（2026-10-01）：引擎原来把模型目录写死成 `E:/qw38`，换机器就找不到。现在引擎侧
优先级已是 `argv[1] > ARK_MODEL_DIR > 明确报错`，但"我该填哪个目录"仍然没人回答。
本工具就是那个回答：扫盘 → 对**必需文件清单**逐项校验 → 打印可用性判决。

为什么不是"引擎自己扫盘找"：盘上可能同时存在 `experts_k3c`（可用）、`experts_k4`、
**`experts_fake3/4`（测试假包）**、`chain`、`experts_k3`。挑错包的现象不是报错，而是
**输出垃圾**（PLE_ROOT 指错那次的老事故：relRMS 0.996 / 14-16 门禁）。所以这里的
原则是「探测 + 校验 + 报告」，绝不自动改写配置。

用法:
    python serve/locate_model.py                     # 扫所有固定盘（深度 3）
    python serve/locate_model.py --root D: E: --depth 4
    python serve/locate_model.py --check E:\\qw38     # 只校验一个目录（手动选择用）
    python serve/locate_model.py --write             # 把最佳候选写进 arkion.local.bat
"""
import argparse
import os
import string
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 必需文件名（来源：src/helm_qw38_gpu2.cu 的实际读取点，已逐条核对）
NEED_FILES = ["nonexp_pack.json", "nonexp_fp8.bin", "nonexp_bf16.bin", "ple_index.json"]
# 参考体积（本机实测，仅用于"看起来对不对"的粗判，不作为硬判据）
REF_MB = {"nonexp_pack.json": 0.13, "nonexp_fp8.bin": 2833.0, "nonexp_bf16.bin": 3770.1,
          "ple_index.json": 0.03}
PACK_HINT = {"experts_k3c": 43680.0, "experts_k4": 58000.0}   # MB 量级参考
NLAYER = 48
FAKE_MARKERS = ("fake", "test", "probe", "stale", "tmp", "bak")


def _mb(path):
    try:
        if os.path.isfile(path):
            return os.path.getsize(path) / 1048576.0
        total = 0
        for r, _d, fs in os.walk(path):
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(r, f))
                except OSError:
                    pass
        return total / 1048576.0
    except OSError:
        return 0.0


def list_packs(d):
    """列出目录下所有 experts_*/ 包，按「应该选谁」排序。

    为什么不能只挑"文件最多"的那个（实测踩过）：本机 `E:/qw38` 下同时有
      experts_k3c(真, 42.7GB) / experts_k4 / experts_k3 / **experts_fake3(假)** /
      **experts_fake4(假)** / chain。它们**同样都是 48/48 完整**，而 os.listdir 的
      字母序让 `experts_fake3` 先出现 ⇒ 只按完整度挑就会挑中假包，现象不是报错，
      而是输出垃圾。所以排序必须带先验：引擎默认包 > 环境指定 > 其他，假名重罚。
    返回 [(name, n_files)]，已按优先级排序。
    """
    try:
        names = os.listdir(d)
    except OSError:
        return []
    env_pack = os.environ.get("K4_PACK_DIR")
    out = []
    for n in names:
        sub = os.path.join(d, n)
        if not os.path.isdir(sub) or not n.startswith("experts_"):
            continue
        have = sum(1 for li in range(NLAYER)
                   if os.path.isfile(os.path.join(sub, "L%02d_%s.bin" % (li, n))))
        pri = 0
        if env_pack and n == env_pack:
            pri += 100                       # 用户显式指定，最高优先
        if n in ("experts_k3c", "experts_k4"):
            pri += 50                        # 引擎的编译期默认包
        if any(m in n.lower() for m in FAKE_MARKERS):
            pri -= 200                       # fake/test/probe/stale/bak
        if n not in PACK_HINT:
            pri -= 10
        out.append((pri, have, n))
    out.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [(n, h) for _p, h, n in out]


def looks_like_model(d):
    """廉价预判：含 nonexp_pack.json 或 ple_index.json 或某个 experts_*/ 子目录。"""
    if not os.path.isdir(d):
        return False
    try:
        for n in os.listdir(d):
            if n in ("nonexp_pack.json", "ple_index.json", "nonexp_fp8.bin"):
                return True
            if n.startswith("experts_") and os.path.isdir(os.path.join(d, n)):
                return True
    except OSError:
        pass
    return False


def validate(d):
    """逐项校验。返回 (verdict, rows, total_mb, pack, pack_ok)。"""
    rows = []
    miss = []
    for f in NEED_FILES:
        p = os.path.join(d, f)
        ok = os.path.isfile(p)
        mb = _mb(p) if ok else 0.0
        ref = REF_MB.get(f, 0.0)
        warn = ""
        if ok and ref and mb < ref * 0.5:
            warn = "  <-- 偏小（参考 %.1f MB）" % ref
        rows.append((f, ok, mb, warn))
        if not ok:
            miss.append(f)
    packs = list_packs(d)
    pack, npack = (packs[0] if packs else (None, 0))
    others = packs[1:]
    pack_ok = (npack == NLAYER)
    total = sum(r[2] for r in rows) + (_mb(os.path.join(d, pack)) if pack else 0.0)
    if miss:
        verdict = "缺文件(%s)" % ",".join(miss[:2])
    elif not pack:
        verdict = "无 experts_* 专家包"
    elif not pack_ok:
        verdict = "专家包不完整(%s %d/%d)" % (pack, npack, NLAYER)
    else:
        verdict = "可用"
    return verdict, rows, total, pack, pack_ok, others


def scan(root, depth):
    """广度受限地找出候选目录（绝不深入模型目录内部，那会是几十万文件）。"""
    out = []
    root = root.rstrip("\\/") + os.sep
    base = len(root.rstrip(os.sep).split(os.sep))
    for r, dirs, _fs in os.walk(root):
        if not r.startswith(root[:3]):
            dirs[:] = []
            continue
        lvl = len(r.rstrip(os.sep).split(os.sep)) - base
        if lvl >= depth:
            dirs[:] = []
        if looks_like_model(r):
            out.append(r)
            dirs[:] = []          # 命中就不往下钻
    return out


def main():
    ap = argparse.ArgumentParser(description="定位并校验模型目录（不自动采纳）")
    ap.add_argument("--root", nargs="*", default=None, help="扫描根（默认全部固定盘）")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--check", default=None, help="只校验这一个目录（手动选择用）")
    ap.add_argument("--write", action="store_true", help="把最佳候选写进 arkion.local.bat")
    a = ap.parse_args()

    if a.check:
        cands = [a.check]
    else:
        roots = a.root
        if not roots:
            roots = ["%s:\\" % c for c in string.ascii_uppercase if os.path.exists("%s:\\" % c)]
        print("[locate] 扫描根: %s  深度=%d（只找候选，不自动采纳）" % (roots, a.depth))
        cands = []
        for rt in roots:
            t0 = time.time()
            got = scan(rt, a.depth)
            print("[locate]   %s -> %d 个候选 (%.1fs)" % (rt, len(got), time.time() - t0))
            cands += got

    if not cands:
        print("[locate] 没找到候选。若模型在别的盘/更深层，用 --root X: --depth N 或直接 --check <目录>")
        return 2

    ranked = []
    for d in cands:
        verdict, rows, total, pack, pack_ok, others = validate(d)
        score = (1 if verdict == "可用" else 0) * 1000 + (1 if pack_ok else 0) * 100 + total / 1000.0
        ranked.append((score, d, verdict, rows, total, pack, pack_ok, others))
    ranked.sort(key=lambda x: -x[0])

    print("\n[locate] ===== 候选（按可用性排序）=====")
    for _s, d, verdict, rows, total, pack, pack_ok, others in ranked:
        print("\n  %-6s %s" % (verdict, d))
        print("         选用的专家包: %s%s   总量: %.1f GB"
              % (pack or "-", "" if pack_ok else " (不完整)", total / 1024.0))
        if others:
            print("         同目录其它包（未选用）: %s"
                  % ", ".join("%s%s" % (n, "" if h == NLAYER else "(%d/%d)" % (h, NLAYER))
                              for n, h in others))
        for f, ok, mb, warn in rows:
            print("         %s %-20s %9.1f MB%s" % ("OK " if ok else "MISS", f, mb, warn))

    best = ranked[0]
    if best[2] != "可用":
        print("\n[locate] 没有完全可用的候选 —— 不要拿不完整的目录去跑，"
              "症状会是加载失败或输出垃圾（后者更危险）。")
        return 1

    print("\n[locate] 建议使用: %s" % best[1])
    print("        set ARK_MODEL_DIR=%s" % best[1])
    print("        （引擎侧优先级：argv[1] > ARK_MODEL_DIR；不再有任何写死的回落路径）")
    if a.write:
        with open("arkion.local.bat", "w", encoding="ascii", newline="\r\n") as f:
            f.write("@echo off\r\n")
            f.write("REM generated by serve/locate_model.py --write\r\n")
            f.write("set ARK_MODEL_DIR=%s\r\n" % best[1])
            if best[5]:
                f.write("set K4_PACK_DIR=%s\r\n" % best[5])
        print("[locate] 已写入 arkion.local.bat（run_serve.bat / run_gate_k3.bat 会自动 call 它）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
