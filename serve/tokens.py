#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve/tokens.py — 常驻分词/模板层（P0 消费层第一块）

设计口径（2026-09-24）：
  · 只依赖 tokenizers + jinja2（与 tools/_mkprompt.py 同源），不引 transformers（省 10s+ 启动）
  · Tokenizer 与已编译模板**只加载一次**（服务常驻，这是与 _mkprompt.py 的本质差别）
  · 多轮 messages + tools + reasoning_effort + enable_thinking + preserve_thinking 全支持
  · 处理三处官方模板硬坑（读 D:/qwen3.8-flash-next/chat_template.jinja 得出，行号即模板行号）：
      ① :136  `tool_call.arguments|items` 要求 arguments 是 **mapping**；
              而 OpenAI 协议里 function.arguments 是 **JSON 字符串** ⇒ 必须 json.loads 后再传。
      ② 模板用 `raise_exception(...)`（:43/:49/:100/:106/:160/:33/:39），
              默认 jinja2 Environment **没有这个全局函数** ⇒ 触发时报 UndefinedError 而非真实原因。
              这里显式注入，让不合法输入给出人话错误。
      ③ :8-29 支持 image/video 占位符，但**引擎只认文本 token** ⇒ 多模态输入必须显式拒绝，
              否则会静默渲染出 <|image_pad|> 让引擎瞎猜。

用法：
  t = Tokens()
  ids = t.encode_messages(messages, tools=None, reasoning_effort="low")
  text = t.decode(ids)
自测（验证多轮+tools渲染 + 前缀稳定性）：
  python serve/tokens.py
"""
import json
import os
import sys

from jinja2 import Environment
from jinja2.exceptions import TemplateError
from tokenizers import Tokenizer

DEFAULT_MODEL_DIR = "D:/qwen3.8-flash-next"
MODEL_DIR = os.environ.get("QWEN_DIR", DEFAULT_MODEL_DIR)
THINK_OPEN, THINK_CLOSE = "<think>", "</think>"


class PromptError(ValueError):
    """不合法请求（对应 HTTP 400）。"""


def _raise_exception(msg):
    raise PromptError(msg)


def _to_mapping_arguments(tool_calls):
    """OpenAI 的 function.arguments 是 JSON 字符串；模板要求 mapping（chat_template.jinja:136）。"""
    fixed = []
    for tc in tool_calls:
        tc = dict(tc)
        fn = dict(tc.get("function") or {})
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                fn["arguments"] = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError as e:
                raise PromptError("tool_calls.function.arguments 不是合法 JSON: %s" % e)
        elif args is None:
            fn["arguments"] = {}
        tc["function"] = fn
        fixed.append(tc)
    return fixed


def assert_text_only(messages):
    """拒绝多模态内容（list 形态的 content）：引擎无视觉通道，接了只会静默出错。"""
    for i, m in enumerate(messages):
        c = m.get("content")
        if isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and ({"image", "image_url", "video"} & set(item)) \
                        or (isinstance(item, dict) and item.get("type") in ("image", "video")):
                    raise PromptError(
                        "messages[%d] 含图片/视频内容：本引擎只支持纯文本，请勿传多模态输入" % i)


class Tokens:
    def __init__(self, model_dir=None):
        self.model_dir = model_dir or MODEL_DIR
        tok_path = os.path.join(self.model_dir, "tokenizer.json")
        tpl_path = os.path.join(self.model_dir, "chat_template.jinja")
        if not os.path.exists(tok_path):
            raise FileNotFoundError("找不到 tokenizer: %s（可用 QWEN_DIR 覆盖）" % tok_path)
        self.tok = Tokenizer.from_file(tok_path)
        self.env = Environment(trim_blocks=True, lstrip_blocks=True)
        self.env.globals["raise_exception"] = _raise_exception
        self.tpl = self.env.from_string(open(tpl_path, encoding="utf-8").read())

    # ---------- 基本 ----------
    def encode(self, text):
        return self.tok.encode(text).ids

    def decode(self, ids):
        return self.tok.decode(ids)

    # ---------- messages -> ids ----------
    def encode_messages(self, messages, tools=None, add_generation_prompt=True,
                        reasoning_effort=None, enable_thinking=None, preserve_thinking=None):
        if not messages:
            raise PromptError("messages 为空")
        assert_text_only(messages)
        msgs = [dict(m) for m in messages]
        for m in msgs:
            if m.get("tool_calls"):
                m["tool_calls"] = _to_mapping_arguments(m["tool_calls"])
            if m.get("content") is None:
                m["content"] = ""
        kw = {"messages": msgs, "add_generation_prompt": add_generation_prompt}
        if tools:
            kw["tools"] = tools
        if reasoning_effort:
            kw["reasoning_effort"] = reasoning_effort      # 模板只认 xhigh/medium/low（:48）
        if enable_thinking is not None:
            kw["enable_thinking"] = enable_thinking
        if preserve_thinking is not None:
            kw["preserve_thinking"] = preserve_thinking
        try:
            rendered = self.tpl.render(**kw)
        except PromptError:
            raise
        except TemplateError as e:
            raise PromptError("模板渲染失败: %s" % e)
        return self.encode(rendered)

    def render(self, *a, **kw):
        """调试用：返回渲染后的文本。"""
        return self.tpl.render(**kw)

    # ---------- 增量解码（SSE 用） ----------
    def stream_decoder(self):
        """返回 push(ids)->新增文本。做法=全量 decode 后取已输出文本的公共前缀之后部分。"""
        state = {"text": "", "n": 0}

        def push(ids):
            text = self.decode(ids)
            # [DEC-FIX 2026-09-28] 必须**扣住结尾的 U+FFFD**，否则会污染 KV 前缀复用。
            #   病根：多字节字符（emoji 等，UTF-8 四字节）被切在两个 token 之间时，HF 的
            #     decode() 对「半截序列」会吐 U+FFFD；而本函数用「与已输出文本求公共前缀、
            #     只发增量」的算法 ⇒ **已发出的 U+FFFD 收不回来**。待该字符补齐后客户端看到的是
            #     `你好！\ufffd👋`（实测引擎日志：`分叉处 期望='你好！👋'` / `实际='你好！\ufffd👋'`）。
            #   后果远不止显示：客户端把这段文本回灌进下一轮 prompt ⇒ 重新编码的 token 序列与
            #     引擎 KV 里的真实序列在 assistant 段分叉 ⇒ **KV 前缀复用整条链失效**，
            #     之后每轮都退化成全量 prefill（用户报的「kv 缓存命中也失效了」）。
            #   修法：结尾的 U+FFFD 一律先不发，等后续 token 补齐后它自然变成真字符。
            #   代价：模型真的以 U+FFFD 收尾时，该字符会被丢掉（可忽略）。
            text = text.rstrip("\ufffd")
            if text == state["text"]:
                return ""
            old = state["text"]
            k = 0
            lim = min(len(old), len(text))
            while k < lim and old[k] == text[k]:
                k += 1
            state["text"], state["n"] = text, len(ids)
            return text[k:]
        return push


# ---------- 输出侧：把生成正文切出 reasoning_content ----------
def split_think(text):
    """引擎产出的是**含 <think> 标记的原始文本**（引擎不解析，只认 EOS）。
    返回 (reasoning_content, content)。未闭合时 reasoning 为全部、content 为空。"""
    if THINK_OPEN not in text:
        return "", text
    head, _, rest = text.partition(THINK_OPEN)
    if THINK_CLOSE in rest:
        rc, _, body = rest.partition(THINK_CLOSE)
        return rc.strip("\n"), body.lstrip("\n")
    return rest, ""


# ================= 自测：多轮 + tools 渲染 + 前缀稳定性 =================
def _selftest():
    t = Tokens()
    print("[1] 模型目录: %s" % t.model_dir)

    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        },
    }]

    sys_msg = {"role": "system", "content": "你是 Arkion，一个本地推理引擎驱动的助手。"}
    u1 = {"role": "user", "content": "北京今天天气怎么样？"}

    ids1 = t.encode_messages([sys_msg, u1], tools=tools, reasoning_effort="low")
    print("[2] 第1轮(tools+low) -> %d ids" % len(ids1))

    # 历史 assistant 的 think 必须回灌到 reasoning_content（模板 :117 会渲染它）
    a1 = {"role": "assistant", "content": "", "reasoning_content": "用户问天气，应该调用工具。",
          "tool_calls": [{"type": "function",
                          "function": {"name": "get_weather", "arguments": '{"city":"beijing"}'}}]}
    tr1 = {"role": "tool", "content": "晴，26 摄氏度，湿度 40%"}
    u2 = {"role": "user", "content": "那需要带伞吗？"}
    ids2 = t.encode_messages([sys_msg, u1, a1, tr1, u2], tools=tools, reasoning_effort="low")
    print("[3] 第2轮(tool_calls+tool) -> %d ids" % len(ids2))

    # 前缀稳定性：第2轮应以「第1轮不加 generation prompt」为前缀（决定 prefix cache 可行性）
    core = t.encode_messages([sys_msg, u1], tools=tools, reasoning_effort="low",
                             add_generation_prompt=False)
    common = 0
    for x, y in zip(core, ids2):
        if x != y:
            break
        common += 1
    print("[4] 前缀复用: common=%d / %d  -> %s"
          % (common, len(core), "PASS 前缀稳定" if common == len(core) else "FAIL 前缀漂移"))

    # 明文预览（截断），确认 tool_calls 的 arguments 被正确展开
    txt = t.tpl.render(messages=[sys_msg, u1, dict(a1, tool_calls=_to_mapping_arguments(a1["tool_calls"])),
                                 tr1, u2], tools=tools, add_generation_prompt=True,
                       reasoning_effort="low")
    print("[5] 渲染尾部 400 字符:")
    print("    " + repr(txt[-400:]))

    rc, content = split_think("<think>先看天气</think>晴天不用带伞")
    print("[6] split_think -> reasoning=%r content=%r" % (rc, content))

    for bad, why in [([{"role": "user", "content": [{"type": "image", "image_url": {"url": "x"}}]}],
                      "多模态拒绝"),
                     ([{"role": "user", "content": "hi"}], "reasoning_effort 值域")]:
        try:
            if why.startswith("reasoning"):
                t.encode_messages(bad, reasoning_effort="highest")
            else:
                t.encode_messages(bad)
            print("[7] %s: FAIL 未拦截" % why)
        except PromptError as e:
            print("[7] %s: PASS (%s)" % (why, str(e)[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
