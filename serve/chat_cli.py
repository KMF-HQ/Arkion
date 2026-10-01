#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve/chat_cli.py — Arkion-Q1 命令行交互（走本机 serve HTTP 服务）

用法:
    python serve/chat_cli.py                        # 连 http://127.0.0.1:8471
    python serve/chat_cli.py --no-think             # 关闭思考（更快，适合指令类任务）
    python serve/chat_cli.py --once "用一句话介绍你自己"
    python serve/chat_cli.py --url http://127.0.0.1:9000 --max-tokens 1024
    python serve/chat_cli.py --system "你是一个简洁的助手"

会话内命令:
    /help             显示帮助
    /exit /quit       退出
    /clear            清空对话历史（保留 system）
    /hist             显示当前历史条数与估算长度
    /think on|off     切换是否显示思考过程
    /nothink          直接以「不思考」模式发送下一条（等价 reasoning_effort=minimal）
    /temp X           设置温度（0 = 贪心）
    /maxtok N         设置 max_tokens
    /system S         重设 system
    /save FILE        把对话存成 json

说明: 请求走**流式**（stream=True），思考过程用暗色显示、正文正常显示；
      服务必须先启动（serve\\run_serve.bat）。P0 无状态 ⇒ 每轮把全量历史重喂。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEF_URL = os.environ.get("ARKION_URL", "http://127.0.0.1:8471")
MODEL = os.environ.get("ARKION_MODEL_ID", "arkion-q1-k3")   # [2026-10-01] 与引擎 /healthz 的 model 一致（原写 k4，与默认 K3 引擎不符）

# [CTX-128K 2026-09-28] 历史预算默认**自动**跟随引擎的 KV 预留（/healthz 的 ctx_limit），
#   不再写死 12000 字符。旧默认 12000 字符约等于 3400 token —— 引擎侧明明是 131072 行
#   （128K），CLI 却先把历史裁到 3.4K ⇒ 用户看到的现象就是「上下文默认还是没有 128k」。
#   预算换算：ctx_limit 减掉生成长度预留，再乘中文经验字/token 比（3.5）。
CHARS_PER_TOKEN = float(os.environ.get("ARKION_CHARS_PER_TOKEN", "3.5"))
GEN_RESERVE_TOKENS = int(os.environ.get("ARKION_GEN_RESERVE", "8192"))


def auto_prompt_chars(ctx_limit):
    """按引擎 KV 预留换算历史字符预算（含思考段）。"""
    tok = max(1024, int(ctx_limit or 131072) - GEN_RESERVE_TOKENS)
    return int(tok * CHARS_PER_TOKEN)


# Windows 终端启用 ANSI（无副作用；非 Windows 忽略）
if os.name == "nt":
    os.system("")

DIM, RESET, CYAN, YELLOW, RED = "\x1b[2m", "\x1b[0m", "\x1b[36m", "\x1b[33m", "\x1b[31m"


def hr(ch="-", n=64):
    return ch * n


class Chat:
    def __init__(self, url, max_tokens=0, temp=1.0, topk=20, top_p=0.95,
                 think=True, show_think=True, system=None, max_prompt_chars=12000,
                 timeout=1800.0):
        self.url = url.rstrip("/")
        self.max_tokens = max_tokens
        self.temp = temp
        self.topk = topk
        self.top_p = top_p
        self.think = think
        self.show_think = show_think
        self.system = system
        self.max_prompt_chars = max_prompt_chars
        self.timeout = timeout
        self.history = []          # [{role, content}]
        if system:
            self.history.append({"role": "system", "content": system})

    # ---------- 服务连通性 ----------
    def healthz(self):
        return json.load(urllib.request.urlopen(self.url + "/healthz", timeout=10))

    def cancel(self):
        """让服务端取消当前生成：**只终止这次生成**，引擎与服务都不退出。"""
        try:
            req = urllib.request.Request(self.url + "/arkion/v1/cancel", data=b"{}",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            return bool(d.get("cancelled"))
        except Exception as e:      # noqa: BLE001
            print("%s[cancel 请求失败] %s%s" % (YELLOW, e, RESET))
            return False

    def wait_ready(self, timeout=900.0):
        """等服务端引擎预热完成（启动后约 10-15s）。已就绪立即返回 True。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.healthz().get("started"):
                    if time.time() - t0 > 1.0:
                        print("%s[引擎就绪 %s]%s" % (DIM, self.healthz().get("ready_seconds"), RESET))
                    return True
            except Exception:
                return False
            print("%s[等待引擎预热 %.0fs…]%s" % (DIM, time.time() - t0, RESET), end="\r", flush=True)
            time.sleep(1.0)
        print()
        return False

    # ---------- 上下文预算（P0 无状态：每轮重喂全量历史） ----------
    @staticmethod
    def _msg_chars(m):
        """一条消息在 prompt 里的实际字符量 —— **必须计入 reasoning_content**。

        [KVREUSE 2026-09-27] 回灌历史思考后，思考段也是 prompt 的一部分（实测本模型
        思考常 6~8k 字，比正文还长）。若只算 content，裁剪判据会严重低估 ⇒ prompt 撑爆
        引擎 KV 预留，请求直接 ERR。
        """
        return len(m.get("content") or "") + len(m.get("reasoning_content") or "")

    def _trim(self):
        """按字符上限裁剪历史（保留 system 与最近若干轮）。返回丢掉的条数。

        ⚠ 裁剪会**打断 KV 前缀复用链**：删掉最早的整轮后，剩余历史不再等于任何已注册
        快照点 ⇒ 本轮退回全量 prefill（服务端随后会为裁剪后的历史重建快照，下一轮恢复命中）。
        这是「上下文不超限」必然的代价，不是 bug。
        """
        if self.max_prompt_chars <= 0:
            return 0
        dropped = 0
        while True:
            n = sum(self._msg_chars(m) for m in self.history)
            if n <= self.max_prompt_chars:
                return dropped
            # 找最老的 user 位置（system 之后）整对丢弃
            start = 1 if self.history and self.history[0]["role"] == "system" else 0
            if len(self.history) - start <= 2:
                return dropped
            del self.history[start:start + 2]
            dropped += 2

    def _body(self, stream=True, nothink=False):
        body = {"model": MODEL, "messages": self.history, "stream": stream,
                "max_tokens": self.max_tokens, "temperature": self.temp}
        if self.topk:
            body["top_k"] = self.topk
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if nothink or not self.think:
            # minimal ⇒ 引擎侧 enable_thinking=False（模板不产生 think 段）
            body["reasoning_effort"] = "minimal"
        return body

    # ---------- 单轮（流式） ----------
    def say(self, text, nothink=False):
        self.history.append({"role": "user", "content": text})
        dropped = self._trim()
        if dropped:
            print("%s[上下文] 已丢弃最早 %d 条历史（字符上限 %d）%s"
                  % (YELLOW, dropped, self.max_prompt_chars, RESET))

        body = self._body(stream=True, nothink=nothink)
        req = urllib.request.Request(self.url + "/v1/chat/completions",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        reasoning, content = "", ""
        t_start = time.time()
        t_first = None
        n_chunks = 0
        in_think = False
        finish = ""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    try:
                        d = json.loads(payload)
                    except Exception:
                        continue
                    if "error" in d:
                        print("%s[服务错误] %s%s" % (RED, d["error"].get("message"), RESET))
                        break
                    _fr = d["choices"][0].get("finish_reason")
                    if _fr:
                        finish = _fr
                    delta = d["choices"][0].get("delta") or {}
                    rc = delta.get("reasoning_content")
                    ct = delta.get("content")
                    if rc:
                        if t_first is None:
                            t_first = time.time()
                        reasoning += rc
                        if self.show_think:
                            if not in_think:
                                print("%s── 思考 ──%s" % (DIM, RESET))
                                in_think = True
                            print(DIM + rc + RESET, end="", flush=True)
                    if ct:
                        if t_first is None:
                            t_first = time.time()
                        if in_think:
                            print("\n%s── 正文 ──%s" % (DIM, RESET))
                            in_think = False
                        content += ct
                        print(ct, end="", flush=True)
                    n_chunks += 1
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8", "replace"))
                msg = err.get("error", {}).get("message", str(e))
            except Exception:
                msg = str(e)
            print("%s[HTTP %s] %s%s" % (RED, e.code, msg, RESET))
            self.history.pop()      # 失败不留在历史里
            return None
        except KeyboardInterrupt:
            # 先通知服务端取消（此时连接还活着，取消才能被引擎收到），随后 with 退出关闭连接，
            # serve 侧的 finally drain 会把引擎残留输出读完 ⇒ 下一个请求不会读到脏数据。
            print("\n%s[中断] 正在通知服务端取消本次生成…%s" % (YELLOW, RESET))
            if self.cancel():
                print("%s[已取消] 引擎在下一个位置边界停下；服务与引擎继续运行%s" % (DIM, RESET))
            else:
                print("%s[取消请求未成功，引擎可能仍在生成]%s" % (YELLOW, RESET))
        except Exception as e:
            print("%s[连接失败] %s%s" % (RED, e, RESET))
            print("  服务是否已启动？  serve\\run_serve.bat")
            self.history.pop()
            return None

        print()
        dt = time.time() - t_start
        ttft = (t_first - t_start) if t_first else 0.0
        out_chars = len(content) + len(reasoning)
        # 生成速度用「首字之后」的时段算（首字延迟 = prefill，两者必须分开看）
        gen_ms = ((dt - ttft) * 1000.0 / max(1, n_chunks - 1)) if n_chunks > 1 else 0.0
        print("%s[总 %.1fs | 首字(prefill) %.1fs | ≈%d tok | 生成 %.0f ms/tok | 思考 %d 字 / 正文 %d 字 | finish=%s]%s"
              % (DIM, dt, ttft, n_chunks, gen_ms, len(reasoning), len(content), finish or "-", RESET))
        if finish == "length":
            print("%s[提示] finish=length = 达到生成长度上限被截断（**不是**模型主动收尾）。"
                  "用 /maxtok 1024 限制本次生成，或调大引擎 SERVER_CTX 再重启服务%s"
                  % (YELLOW, RESET))

        if content or reasoning:
            # [KVREUSE 2026-09-27] **必须回灌思考段**（reasoning_content），否则 KV 前缀
            #   复用必然失效：引擎 KV 里 assistant 段是 `<think>思考</think>正文`，而丢掉思考
            #   重编码出来的是 `正文` ⇒ 从 assistant 段第一处就分叉。
            #   离线实测（_diag_prefix.py）：不回流 prefix 命中 61/107；回流后 107/107（整段命中）。
            #   代价是思考段进 prompt（模板 :117 会渲染）⇒ 上下文增长更快，由 _trim 兜底。
            a_msg = {"role": "assistant", "content": content}
            if reasoning:
                a_msg["reasoning_content"] = reasoning
            self.history.append(a_msg)
        else:
            self.history.pop()
        return content, reasoning


HELP = """{c}命令{r}
  /help              本帮助
  /exit, /quit       退出
  /clear             清空历史（保留 system）
  /hist              历史条数与长度
  /think on|off      开关「显示思考过程」
  /nothink           下一条以不思考模式发送
  /thinkmode on|off  开关「是否思考」（关 = reasoning_effort minimal）
  /temp X            温度（0 = 贪心）
  /maxtok N          max_tokens（注意给思考留预算，建议 >=256）
  /system S          重设 system（并清空历史）
  /save FILE         保存对话 json
""".format(c=CYAN, r=RESET)


def _repl(chat):
    print(hr("="))
    print("Arkion-Q1 交互（%s）  /help 看命令，/exit 退出" % chat.url)
    print(hr("="))
    nothink_once = False
    while True:
        try:
            line = input("\n%s你 >%s " % (CYAN, RESET))
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        s = line.strip()
        if not s:
            continue
        if s.startswith("/"):
            cmd, _, arg = s[1:].partition(" ")
            cmd = cmd.lower()
            arg = arg.strip()
            if cmd in ("exit", "quit"):
                return 0
            if cmd == "help":
                print(HELP)
            elif cmd == "clear":
                chat.history = [{"role": "system", "content": chat.system}] if chat.system else []
                print("%s[已清空历史]%s" % (DIM, RESET))
            elif cmd == "hist":
                n = sum(chat._msg_chars(m) for m in chat.history)
                print("%s[历史 %d 条 / %d 字符（含思考）/ 上限 %d]%s"
                      % (DIM, len(chat.history), n, chat.max_prompt_chars, RESET))
                for i, m in enumerate(chat.history):
                    extra = " +思考%d字" % len(m["reasoning_content"]) if m.get("reasoning_content") else ""
                    print("  %2d %-9s %s%s"
                          % (i, m["role"], (m.get("content") or "")[:50].replace("\n", " "), extra))
            elif cmd == "think":
                chat.show_think = (arg == "on")
                print("%s[显示思考 = %s]%s" % (DIM, chat.show_think, RESET))
            elif cmd == "thinkmode":
                chat.think = (arg == "on")
                print("%s[是否思考 = %s]%s" % (DIM, chat.think, RESET))
            elif cmd == "nothink":
                nothink_once = True
                print("%s[下一条不思考]%s" % (DIM, RESET))
            elif cmd == "temp":
                try:
                    chat.temp = float(arg)
                    chat.topk = 1 if chat.temp <= 0 else 20
                    print("%s[temp=%s top_k=%d]%s" % (DIM, chat.temp, chat.topk, RESET))
                except ValueError:
                    print("%s用法: /temp 0.7%s" % (YELLOW, RESET))
            elif cmd == "maxtok":
                try:
                    chat.max_tokens = int(arg)
                    print("%s[max_tokens=%d]%s" % (DIM, chat.max_tokens, RESET))
                except ValueError:
                    print("%s用法: /maxtok 512%s" % (YELLOW, RESET))
            elif cmd == "system":
                chat.system = arg
                chat.history = [{"role": "system", "content": arg}] if arg else []
                print("%s[system 已重设，历史清空]%s" % (DIM, RESET))
            elif cmd == "save":
                fn = arg or "arkion_chat.json"
                with open(fn, "w", encoding="utf-8") as f:
                    json.dump(chat.history, f, ensure_ascii=False, indent=2)
                print("%s[已保存 %s]%s" % (DIM, fn, RESET))
            else:
                print("%s未知命令 %s（/help）%s" % (YELLOW, s, RESET))
            continue
        chat.say(s, nothink=nothink_once)
        nothink_once = False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Arkion-Q1 命令行交互")
    ap.add_argument("--url", default=DEF_URL, help="服务地址（默认 %s）" % DEF_URL)
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="生成长度上限；0 = 不限（按引擎剩余 KV 生成到 EOS，默认）")
    ap.add_argument("--temp", type=float, default=1.0)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--no-think", action="store_true", help="关闭思考（reasoning_effort=minimal）")
    ap.add_argument("--hide-think", action="store_true", help="只隐藏思考显示（仍思考）")
    ap.add_argument("--system", default=None)
    ap.add_argument("--max-prompt-chars", type=int, default=0,
                    help="历史长度上限（字符，含思考段）；超出按最老整轮丢弃。"
                         "0 = 自动（默认）：按引擎 /healthz 的 ctx_limit 换算，"
                         "即 ctx_limit 减去 %d token 的生成预留、再乘约 %.1f 字符/token。"
                         "128K 引擎下约 43 万字符；写死的小值（如旧的 12000）会把上下文"
                         "先裁到 3.4K，看起来就像「上下文没有 128K」。" % (GEN_RESERVE_TOKENS, CHARS_PER_TOKEN))
    ap.add_argument("--once", default=None, help="单发模式：直接问一句并退出")
    a = ap.parse_args(argv)

    chat = Chat(a.url, max_tokens=a.max_tokens, temp=a.temp, topk=a.top_k, top_p=a.top_p,
                think=not a.no_think, show_think=not a.hide_think, system=a.system,
                max_prompt_chars=a.max_prompt_chars)
    try:
        h = chat.healthz()
    except Exception as e:
        print("%s无法连接 %s：%s%s" % (RED, chat.url, e, RESET))
        print("请先启动服务：  serve\\run_serve.bat")
        return 2
    if os.environ.get("ARKION_VERBOSE"):
        print("%s[healthz] %s%s" % (DIM, h, RESET))
    if not h.get("started"):
        chat.wait_ready()

    # [CTX-128K 2026-09-28] 历史预算默认跟随引擎 KV 预留（未显式给 --max-prompt-chars 时）。
    chat.ctx_limit = int(h.get("ctx_limit") or 0)
    if a.max_prompt_chars and a.max_prompt_chars > 0:
        chat.max_prompt_chars = int(a.max_prompt_chars)
        _src = "命令行指定"
    else:
        chat.max_prompt_chars = auto_prompt_chars(chat.ctx_limit)
        _src = "自动跟随引擎 ctx_limit"
    print("%s[上下文] 引擎 KV 预留 %d token | 历史预算 %.0f 字符（%s，约 %.1fK token）%s"
          % (DIM, chat.ctx_limit, chat.max_prompt_chars, _src,
             chat.max_prompt_chars / CHARS_PER_TOKEN / 1000.0, RESET))

    if a.once is not None:
        chat.say(a.once)
        return 0
    return _repl(chat)


if __name__ == "__main__":
    sys.exit(main())
