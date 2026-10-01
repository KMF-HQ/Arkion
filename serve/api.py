#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve/api.py — OpenAI 兼容面（schema / 校验 / SSE / 错误体）

★ 本文件**不得**命名为 openai.py：它在 sys.path 首位时会遮蔽官方 openai 包
  （实测：`from openai import OpenAI` 会 ImportError）。故取名 api.py。

设计口径（P0，2026-09-24）:
  · **端点路径必须是 /v1/*** —— 这是生态入口，改名等于放弃生态，也就放弃了做这个端口的理由。
    arkion 的品牌痕迹放在：model 字段值、/arkion/v1/* 扩展端点、X-Arkion-* 响应头、arkion_* 扩展字段。
  · 不支持的能力**显式 400**，不静默忽略 —— 静默忽略会误导用户（他以为生效了）。
    唯一的例外见 validate(): 值为「中性」时放行（客户端常自动带 penalty=0）。
  · 值为 0 / None 的采样或惩罚参数一律放行，避免无意义的不兼容。
"""
import json
import os
import time
import uuid
from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field

MODEL_ID = os.environ.get("ARKION_MODEL_ID", "arkion-q1-k3")
BASE_MODEL = "qwen3.8-flash-next"
ENGINE_TAG = "arkion-q1"
# 默认「不限」：0 表示由服务端按引擎剩余 KV 容量补满（真无限会越过 KV 上限导致引擎退出）。
DEFAULT_MAX_NEW = int(os.environ.get("ARKION_MAX_NEW", "0"))
STRICT_MODEL = os.environ.get("ARKION_STRICT_MODEL") == "1"

# OpenAI 的 reasoning_effort 值域与 Qwen 模板不同（模板只认 xhigh/medium/low，见模板 :48）。
#   minimal → 不思考（enable_thinking=False）
#   low/medium/high → low/medium/xhigh
REASONING_MAP = {"minimal": ("low", False), "low": ("low", None),
                 "medium": ("medium", None), "high": ("xhigh", None),
                 "xhigh": ("xhigh", None)}

# [THINK-DEFAULT 2026-10-01] 请求**没给** reasoning_effort 时的默认档 = 「普通」(medium)。
#   原行为：保持 None ⇒ 交给 chat_template.jinja 自己的默认（= xhigh）⇒ 实测在
#   max_tokens 偏小时预算会被思考全部吃掉（正文 0 字、finish=length），
#   用户看到的是"模型没回答"而不是"想太久了"。
#   档位对照（模板只认 xhigh/medium/low）：minimal=不思考 / low / medium=普通 / high|xhigh。
#   覆盖方式：环境变量 ARKION_DEFAULT_EFFORT，或请求级显式 reasoning_effort。
DEFAULT_EFFORT = os.environ.get("ARKION_DEFAULT_EFFORT", "medium").lower()


class APIError(Exception):
    def __init__(self, message, status=400, err_type="invalid_request_error",
                 code=None, param=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.err_type = err_type
        self.code = code
        self.param = param

    def body(self):
        return {"error": {"message": self.message, "type": self.err_type,
                          "code": self.code, "param": self.param}}


# ---------------- schema ----------------
class ChatMessage(BaseModel):
    role: str
    content: Optional[Any] = None
    name: Optional[str] = None
    tool_calls: Optional[List[dict]] = None
    tool_call_id: Optional[str] = None
    # 自研扩展：把历史思考段回灌（不传也能跑，但多轮质量与 KV 前缀复用都受益）
    reasoning_content: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None                     # 非标准：直接给引擎 topk
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    n: Optional[int] = 1
    stop: Optional[Union[str, List[str]]] = None
    seed: Optional[int] = None
    tools: Optional[List[dict]] = None
    tool_choice: Optional[Any] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    reasoning_effort: Optional[str] = None
    stream_options: Optional[dict] = None
    # ---- 自研扩展字段（arkion_*）----
    arkion_enable_thinking: Optional[bool] = None
    arkion_preserve_thinking: Optional[bool] = None
    arkion_session_id: Optional[str] = None

    class Config:
        extra = "ignore"        # 未知字段静默忽略 ⇒ 标准客户端能直接连


def _dump(m):
    return m.model_dump() if hasattr(m, "model_dump") else m.dict()


def validate(req: ChatCompletionRequest):
    """-> (ids 构造所需参数 dict)。不合规直接抛 APIError(400)。"""
    if not req.messages:
        raise APIError("messages 不能为空", param="messages")
    if req.model and STRICT_MODEL and req.model != MODEL_ID:
        raise APIError("model %r 不存在，本服务只提供 %r" % (req.model, MODEL_ID),
                       code="model_not_found", param="model")
    if req.n not in (None, 1):
        raise APIError("不支持 n>1（引擎为单序列解码，无并行采样）", param="n")
    if req.logprobs or req.top_logprobs:
        raise APIError("不支持 logprobs：引擎只在 head 段做 argmax，不保留全量 logits",
                       param="logprobs")
    for name in ("presence_penalty", "frequency_penalty"):
        v = getattr(req, name)
        if v:
            raise APIError("不支持 %s：引擎未实现重复惩罚（传 0 或省略）" % name, param=name)
    if req.tool_choice not in (None, "auto", "none"):
        raise APIError("tool_choice 仅支持 auto/none（工具调用解析在 P1）", param="tool_choice")

    # [THINK-DEFAULT 2026-10-01] 未显式指定时用 DEFAULT_EFFORT（默认 medium/普通），
    #   不再落到模板的 xhigh 默认。
    _effort = req.reasoning_effort or DEFAULT_EFFORT
    reasoning_effort, enable_thinking = None, req.arkion_enable_thinking
    if _effort:
        key = str(_effort).lower()
        if key not in REASONING_MAP:
            raise APIError("reasoning_effort 仅支持 %s（OpenAI 风格）或 xhigh/medium/low（Qwen 原生）"
                           % "/".join(sorted(REASONING_MAP)), param="reasoning_effort")
        reasoning_effort, et = REASONING_MAP[key]
        if et is False:
            enable_thinking = False

    # 采样参数映射：OpenAI 无 top_k ⇒ temperature==0 视为贪心（topk=1），否则默认 topk=20
    temp = 1.0 if req.temperature is None else float(req.temperature)
    topk = req.top_k if req.top_k else (1 if temp <= 0.0 else 20)
    top_p = 0.95 if req.top_p is None else float(req.top_p)
    if temp <= 0.0:
        temp = 1.0        # 引擎 temp=0 无意义，贪心靠 topk=1 表达
    # max_tokens 省略或 <=0 ⇒ **不限**：由 engine.generate 按剩余 KV 容量补满
    #   （引擎 KV 在启动期定格，真无限会越界；「补满」等价于「一直生成到 EOS 或上下文用尽」）
    max_new = req.max_completion_tokens or req.max_tokens or DEFAULT_MAX_NEW
    if max_new is None or int(max_new) < 1:
        max_new = 0

    stops = req.stop
    if isinstance(stops, str):
        stops = [stops]
    stops = [s for s in (stops or []) if s]

    return dict(reasoning_effort=reasoning_effort, enable_thinking=enable_thinking,
                preserve_thinking=req.arkion_preserve_thinking, temp=temp, topk=topk,
                top_p=top_p, max_new=max_new, stops=stops,
                thinking=(enable_thinking is not False))


# ---------------- 响应构造 ----------------
def _cid():
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def response_body(cid, model, content, reasoning_content, finish_reason, n_prompt, n_completion):
    return {
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content,
                        **({"reasoning_content": reasoning_content} if reasoning_content else {})},
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": n_prompt, "completion_tokens": n_completion,
                  "total_tokens": n_prompt + n_completion},
        "system_fingerprint": ENGINE_TAG,
    }


def chunk(cid, model, delta, finish_reason=None):
    obj = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
           "model": model, "choices": [{"index": 0, "delta": delta,
                                        "finish_reason": finish_reason}]}
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def usage_chunk(cid, model, prompt_tokens, completion_tokens):
    """流式 usage 尾包（stream_options.include_usage=true 时发送）"""
    obj = {
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


DONE = "data: [DONE]\n\n"


def models_body():
    return {"object": "list", "data": [{
        "id": MODEL_ID, "object": "model", "created": 1758672000, "owned_by": "arkion",
        "arkion_base_model": BASE_MODEL, "arkion_engine": ENGINE_TAG,
        "arkion_quant": MODEL_ID.rsplit("-", 1)[-1],
    }]}


# ---------------- stop 序列过滤（安全边界，避免多吐字符） ----------------
class StopFilter:
    """缓冲 max(len(stop))-1 个字符，保证一旦命中 stop 就不会已经发出去多余字符。"""

    def __init__(self, stops):
        self.stops = [s for s in (stops or []) if s]
        self.keep = max((len(s) for s in self.stops), default=0) - 1
        self.buf = ""
        self.hit = False

    def feed(self, text):
        if not text or self.hit:
            return ""
        self.buf += text
        for s in self.stops:
            i = self.buf.find(s)
            if i >= 0:
                self.buf = self.buf[:i]
                self.hit = True
                return self.buf
        if len(self.buf) > self.keep:
            out = self.buf[:len(self.buf) - self.keep] if self.keep > 0 else self.buf
            self.buf = self.buf[len(out):]
            return out
        return ""

    def flush(self):
        if self.hit:
            self.hit = False
            return ""
        out, self.buf = self.buf, ""
        return out
