#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arkion-cli — Arkion-Q1 命令行工具

用法:
    python arkion-cli.py status            查看服务状态
    python arkion-cli.py models            列出模型
    python arkion-cli.py chat "你好"       单次对话
    python arkion-cli.py chat              交互式对话
    python arkion-cli.py bench             快速速度测试
    python arkion-cli.py --help            帮助
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

# [CLI-ENC 2026-10-01] 控制台默认是 CP936，而本文件大量打印 ❌/✅/💭 等**不在 CP936 里的字符**
#   ⇒ print 自身抛 UnicodeEncodeError，把友好提示变成裸栈（实测 `status` 在服务未运行时就是
#   这个现象：先抛 URLError、被捕获后打印 ❌ 又崩，用户看到的是 urllib 的 traceback）。
#   chat_cli.py 一直有这个重编码，本文件漏了。errors=replace 保证任何码页下都不再崩。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_URL = os.environ.get("ARKION_API_URL", "http://127.0.0.1:8471/v1")
DEFAULT_MODEL = os.environ.get("ARKION_MODEL", "arkion-q1-k3")


def _post(path, payload, timeout=120):
    url = DEFAULT_URL.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer sk-arkion-local"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            raise RuntimeError("HTTP %d: %s" % (e.code, err.get("error", {}).get("message", body)))
        except Exception:
            raise RuntimeError("HTTP %d: %s" % (e.code, body[:200]))


def _get(path, timeout=30):
    url = DEFAULT_URL.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Authorization": "Bearer sk-arkion-local"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %d: %s" % (e.code, body[:200]))


def cmd_status(args):
    """查看服务状态"""
    print("=" * 60)
    print("Arkion-Q1 服务状态")
    print("=" * 60)
    print("  API 地址: %s" % DEFAULT_URL)

    # 1. 健康检查（healthz 在根路径）
    base_url = DEFAULT_URL
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    elif base_url.endswith("/v1/"):
        base_url = base_url[:-4]
    health_url = base_url.rstrip("/") + "/healthz"
    try:
        req = urllib.request.Request(health_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        print("  状态: ✅ 运行中")
        for k, v in health.items():
            if k == "ok":
                continue
            print("  %s: %s" % (k, v))
    except Exception as e:
        print("  状态: ❌ 无法连接 (%s)" % e)
        print()
        print("  请先启动服务: serve\\run_serve.bat")
        return 1

    # 2. 模型列表
    print()
    try:
        models = _get("/models")
        print("  可用模型:")
        for m in models.get("data", []):
            print("    - %s" % m["id"])
            extras = {k: v for k, v in m.items() if k.startswith("arkion_")}
            for ek, ev in extras.items():
                print("        %s: %s" % (ek, ev))
    except Exception as e:
        print("  模型列表: ❌ %s" % e)

    # 3. 快速测速
    print()
    print("  速度测试中...")
    try:
        t0 = time.time()
        resp = _post("/chat/completions", {
            "model": DEFAULT_MODEL,
            "messages": [{"role": "user", "content": "pong"}],
            "max_tokens": 5,
            "temperature": 0.0,
            "reasoning_effort": "minimal",
        }, timeout=60)
        dt = time.time() - t0
        n_out = resp["usage"]["completion_tokens"]
        print("  首包延迟: %.1fs" % dt)
        print("  生成 tokens: %d" % n_out)
        print("  模型: %s" % resp["model"])
    except Exception as e:
        print("  速度测试失败: %s" % e)

    print()
    return 0


def cmd_models(args):
    """列出可用模型"""
    try:
        models = _get("/models")
        print("可用模型:")
        for m in models.get("data", []):
            print()
            print("  %s" % m["id"])
            print("    owned_by: %s" % m.get("owned_by", "-"))
            for k, v in m.items():
                if k.startswith("arkion_"):
                    print("    %s: %s" % (k, v))
        print()
    except Exception as e:
        print("❌ 获取模型列表失败: %s" % e)
        return 1
    return 0


def cmd_chat(args):
    """对话（单次或交互模式）"""
    if args.message:
        # 单次对话
        _chat_once(args.message, args.stream, args.reasoning, args.max_tokens)
        return 0

    # 交互模式
    print("Arkion-Q1 交互式对话（输入 exit 退出，clear 清屏）")
    print("  模型: %s  |  思考: %s  |  流式: %s" % (
        args.model or DEFAULT_MODEL,
        args.reasoning or "medium(默认)",
        "开" if args.stream else "关"))
    print("-" * 60)

    messages = []
    while True:
        try:
            user_input = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print("再见！")
            break
        if user_input.lower() in ("clear", "cls"):
            messages = []
            os.system("cls" if os.name == "nt" else "clear")
            print("已清空对话历史")
            continue
        if user_input.lower() == "history":
            for i, m in enumerate(messages):
                print("  [%d] %s: %s" % (i, m["role"], m["content"][:50]))
            continue

        messages.append({"role": "user", "content": user_input})
        try:
            reply = _chat_once(user_input, args.stream, args.reasoning,
                              args.max_tokens, messages, show_prompt=False)
            messages.append({"role": "assistant", "content": reply})
        except Exception as e:
            print("❌ 错误: %s" % e)
            messages.pop()  # 回滚失败的消息

    return 0


def _chat_once(message, stream=False, reasoning=None, max_tokens=1024, messages=None,
               show_prompt=True):
    """单次对话，返回回复内容"""
    if messages is None:
        messages = [{"role": "user", "content": message}]
    else:
        # messages 已经包含所有历史
        pass

    payload = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": stream,
    }
    if reasoning:
        payload["reasoning_effort"] = reasoning

    if show_prompt:
        print()

    if not stream:
        # 非流式
        if show_prompt:
            print("思考中...", end=" ", flush=True)
        t0 = time.time()
        resp = _post("/chat/completions", payload, timeout=1800)
        dt = time.time() - t0
        content = resp["choices"][0]["message"]["content"]
        reasoning_content = resp["choices"][0]["message"].get("reasoning_content", "")
        usage = resp["usage"]

        if show_prompt:
            print("(%.1fs)" % dt)
            print()
            if reasoning_content:
                print("💭 思考:")
                print(_indent(reasoning_content, "  "))
                print()
                print("📝 回答:")
            print(_indent(content, "  "))
            print()
            print("  prompt=%d completion=%d total=%d  finish=%s" % (
                usage["prompt_tokens"], usage["completion_tokens"],
                usage["total_tokens"], resp["choices"][0]["finish_reason"]))
        return content
    else:
        # 流式
        url = DEFAULT_URL.rstrip("/") + "/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer sk-arkion-local",
                     "Accept": "text/event-stream"},
            method="POST",
        )
        full_content = ""
        full_reasoning = ""
        t0 = time.time()
        first_token_time = None

        if show_prompt:
            print("💭 思考中..." if (reasoning and reasoning != "minimal") else "生成中...")
            print()

        with urllib.request.urlopen(req, timeout=1800) as resp:
            buffer = ""
            for line_bytes in resp:
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("choices"):
                        delta = chunk["choices"][0].get("delta", {})
                        if "reasoning_content" in delta and delta["reasoning_content"]:
                            full_reasoning += delta["reasoning_content"]
                            if show_prompt:
                                # 思考阶段先显示
                                pass
                        if "content" in delta and delta["content"]:
                            if first_token_time is None:
                                first_token_time = time.time() - t0
                                if show_prompt and full_reasoning:
                                    print()
                                    print("📝 回答:")
                            full_content += delta["content"]
                            if show_prompt:
                                print(delta["content"], end="", flush=True)

        if show_prompt:
            print()
            print()
            dt = time.time() - t0
            print("  总时间: %.1fs | 首token: %.1fs | 生成: %d tokens" % (
                dt, first_token_time or 0, len(full_content) // 2))  # 粗估
        return full_content


def _indent(text, prefix="  "):
    return "\n".join(prefix + line for line in text.split("\n"))


def cmd_config(args):
    """查看/设置运行时配置"""
    print("=" * 60)
    print("Arkion-Q1 运行时配置")
    print("=" * 60)
    print()

    try:
        base_url = DEFAULT_URL
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        elif base_url.endswith("/v1/"):
            base_url = base_url[:-4]
        config_url = base_url.rstrip("/") + "/arkion/v1/config"
        req = urllib.request.Request(config_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cfg = data.get("config", {})

        print("  【模型】")
        print("    model:           %s" % cfg.get("model", "-"))
        print("    base_model:      %s" % cfg.get("base_model", "-"))
        print("    engine:          %s" % cfg.get("engine", "-"))
        print("    engine_exe:      %s" % cfg.get("engine_exe", "-"))
        print()

        print("  【KV 缓存】")
        print("    kv_reuse:        %s" % ("开" if cfg.get("kv_reuse") else "关"))
        print("    kv_sys_snap:     %s" % ("开" if cfg.get("kv_sys_snap") else "关"))
        print("    snap_budget_mb:  %s" % cfg.get("snap_budget_mb", "-"))
        print("    ctx_limit:       %s 行" % cfg.get("ctx_limit", "-"))
        print()

        print("  【思考】")
        print("    默认思考模板:    %s" % ("开" if cfg.get("default_think_template") else "关"))
        print()

        print("  【服务】")
        print("    地址:            http://%s:%s" % (cfg.get("host", "-"), cfg.get("port", "-")))
        print("    max_tokens:      %s" % cfg.get("default_max_new", "-"))
        print()

        print("-" * 60)
        print("  提示: 配置通过环境变量设置，修改后需重启服务")
        print()
        print("  常用环境变量:")
        print("    ARKION_EXE                 引擎 exe 路径（切换 K3/K4）")
        print("    ARKION_MODEL_ID            模型名")
        print("    ARKION_PORT                端口（默认 8471）")
        print("    ARKION_DEFAULT_THINK_TEMPLATE  默认思考模板（1=开 0=关）")
        print("    ARKION_KV_REUSE            KV 缓存复用（1=开 0=关）")
        print("    ARKION_SNAP_BUDGET_MB      快照预算 MB（默认 3072）")
        print("    SERVER_CTX                 KV 预留行数（引擎默认 4096；serve\\run_serve.bat 默认 65536）")
        print("    ARK_MODEL_DIR              模型目录（原为写死的 E:/qw38）")
        print("    ARK_PLE_ROOT               PLE 分片根目录（原为写死的 D:/qwen3.8-flash-next）")
        print("    ARK_GOLDEN_DIR             golden 数据目录（默认 data/golden）")
        print()
    except Exception as e:
        print("  ❌ 获取配置失败: %s" % e)
        return 1

    return 0


def cmd_bench(args):
    """快速速度测试"""
    print("=" * 60)
    print("Arkion-Q1 速度测试")
    print("=" * 60)
    print("  模型: %s" % DEFAULT_MODEL)
    print()

    prompts = [
        ("短回复 (约20 token)", "用一句话介绍你自己。", 30, "minimal"),
        ("中回复 (约100 token)", "用100字左右解释什么是Transformer。", 150, "minimal"),
        ("思考模式 (约200 token)", "简单解释一下为什么天空是蓝色的。", 300, "medium"),
    ]

    for label, prompt, max_tok, reasoning in prompts:
        print("  测试: %s..." % label)
        t0 = time.time()
        try:
            resp = _post("/chat/completions", {
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tok,
                "temperature": 0.0,
                "reasoning_effort": reasoning,
            }, timeout=600)
            dt = time.time() - t0
            n_prompt = resp["usage"]["prompt_tokens"]
            n_completion = resp["usage"]["completion_tokens"]
            ms_per_tok = (dt / n_completion * 1000) if n_completion else 0
            print("    ✅ 时间: %.1fs | prompt=%d | completion=%d | %.1f ms/tok" % (
                dt, n_prompt, n_completion, ms_per_tok))
        except Exception as e:
            print("    ❌ 失败: %s" % e)
        print()

    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="arkion-cli",
        description="Arkion-Q1 推理引擎命令行工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python arkion-cli.py status              查看服务状态
  python arkion-cli.py config              查看运行时配置
  python arkion-cli.py models              列出可用模型
  python arkion-cli.py chat "你好"         单次对话
  python arkion-cli.py chat                交互式对话
  python arkion-cli.py chat --stream       流式对话
  python arkion-cli.py bench               速度测试
  python arkion-cli.py chat -r high        深度思考模式

环境变量:
  ARKION_API_URL    API 地址 (默认 http://127.0.0.1:8471/v1)
  ARKION_MODEL      默认模型名 (默认 arkion-q1-k3)
""")

    sub = parser.add_subparsers(dest="command", help="命令")

    # status
    sub.add_parser("status", help="查看服务状态")

    # config
    sub.add_parser("config", help="查看运行时配置")

    # models
    sub.add_parser("models", help="列出可用模型")

    # chat
    p_chat = sub.add_parser("chat", help="对话")
    p_chat.add_argument("message", nargs="?", help="消息内容（省略则进入交互模式）")
    p_chat.add_argument("-s", "--stream", action="store_true", help="流式输出")
    p_chat.add_argument("-r", "--reasoning", default=None,
                        help="思考深度: minimal/low/medium/high (默认medium)")
    p_chat.add_argument("-m", "--model", default=None, help="模型名")
    p_chat.add_argument("-n", "--max-tokens", type=int, default=1024,
                        help="最大生成 token 数 (默认 1024)")

    # bench
    p_bench = sub.add_parser("bench", help="速度测试")
    p_bench.add_argument("-r", "--reasoning", default=None, help="思考深度")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    # [CLI-GUARD 2026-10-01] 顶层兜底：任何子命令里漏网的网络错误都变成一句可读提示，
    #   而不是把 urllib 的 traceback 摔给用户（服务未启动是最常见的第一次使用场景）。
    try:
        if args.command == "status":
            return cmd_status(args)
        elif args.command == "config":
            return cmd_config(args)
        elif args.command == "models":
            return cmd_models(args)
        elif args.command == "chat":
            if args.model:
                global DEFAULT_MODEL
                DEFAULT_MODEL = args.model
            return cmd_chat(args)
        elif args.command == "bench":
            return cmd_bench(args)
        else:
            parser.print_help()
            return 1
    except urllib.error.URLError as e:
        print("无法连接 %s：%s" % (DEFAULT_URL, e))
        print("请先启动服务:  serve\\run_serve.bat   （或 serve\\chat.bat，它会自动拉起）")
        return 2
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main() or 0)
