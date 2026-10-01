#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve/think.py — 增量思考段切分（自研思考模式的核心交付物）

背景（为什么必须有这一层）:
  · 引擎**不解析 think 标记**，只认两个 EOS（248046 <|im_end|> / 248044 <|endoftext|>）
    ⇒ 模型直接吐出「思考内容 + </think> + 正文」的原始文本。
  · 官方模板在 add_generation_prompt 时**已经替我们写好了开标记**（chat_template.jinja:163-169）:
        enable_thinking = true  → prompt 尾部是 `<|im_start|>assistant\n<think>\n`
        enable_thinking = false → prompt 尾部是 `<|im_start|>assistant\n<think>\n\n</think>\n\n`
    ⇒ **模型输出里不会出现开头的 `<think>`**，状态机必须以「已在思考中」为初始态；
       而 enable_thinking=false 时初始态直接是正文。
  · 流式下标记可能跨 chunk（`</thi` + `nk>`）⇒ 必须缓冲「可能是标记前缀」的尾巴。

输出契约: feed(delta_text) -> [(channel, text), ...]，channel ∈ {"reasoning","content"}
"""
CLOSE = "</think>"


class ThinkSplitter:
    def __init__(self, thinking=True, strip_lead_newlines=True):
        # thinking=False 表示 prompt 已闭合 think（enable_thinking=false），输出全是正文
        self.state = "reasoning" if thinking else "content"
        self.buf = ""
        self.strip_lead_newlines = strip_lead_newlines
        self._at_content_start = True

    @staticmethod
    def _suffix_prefix_len(buf, tag):
        """buf 结尾有多少字符是 tag 的前缀（用于防止标记被切成两块）。"""
        m = min(len(buf), len(tag) - 1)
        for k in range(m, 0, -1):
            if buf[-k:] == tag[:k]:
                return k
        return 0

    def feed(self, delta):
        if not delta:
            return []
        self.buf += delta
        out = []
        while self.buf:
            if self.state == "reasoning":
                i = self.buf.find(CLOSE)
                if i >= 0:
                    if i:
                        out.append(("reasoning", self.buf[:i]))
                    self.buf = self.buf[i + len(CLOSE):]
                    self.state = "content"
                    self._at_content_start = True
                    continue
                keep = self._suffix_prefix_len(self.buf, CLOSE)
                if keep:
                    if len(self.buf) > keep:
                        out.append(("reasoning", self.buf[:-keep]))
                        self.buf = self.buf[-keep:]
                    break
                out.append(("reasoning", self.buf))
                self.buf = ""
                break
            else:
                text = self.buf
                self.buf = ""
                if self._at_content_start and self.strip_lead_newlines:
                    text = text.lstrip("\n")
                    if not text:
                        break
                if text:
                    self._at_content_start = False
                    out.append(("content", text))
                break
        return out

    def flush(self):
        """流结束时调用：把残留（可能是不完整的标记前缀）按当前通道吐出。"""
        if not self.buf:
            return []
        ch = "reasoning" if self.state == "reasoning" else "content"
        out = [(ch, self.buf)]
        self.buf = ""
        return out


if __name__ == "__main__":
    # 1) 全程思考后转正文
    s = ThinkSplitter(thinking=True)
    got = []
    for piece in ["先分析", "问题。", "\n</thi", "nk>", "\n\n", "答案是", "42。"]:
        got += s.feed(piece)
    got += s.flush()
    txt = {"reasoning": "", "content": ""}
    for ch, t in got:
        txt[ch] += t
    assert txt["reasoning"] == "先分析问题。\n", repr(txt)
    assert txt["content"] == "答案是42。", repr(txt)

    # 2) 不思考模式（prompt 已闭合）⇒ 全部是正文
    s2 = ThinkSplitter(thinking=False)
    got2 = []
    for piece in ["直接", "回答"]:
        got2 += s2.feed(piece)
    got2 += s2.flush()
    assert [c for c, _ in got2] == ["content", "content"], got2
    assert "".join(t for _, t in got2) == "直接回答"

    # 3) 从未闭合（被 max_tokens 截断）⇒ 全在 reasoning
    s3 = ThinkSplitter(thinking=True)
    g3 = s3.feed("想了一半") + s3.flush()
    assert [c for c, _ in g3] == ["reasoning"], g3

    print("think.py 自测 PASS")
