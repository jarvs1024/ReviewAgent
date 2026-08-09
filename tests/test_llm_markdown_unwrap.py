"""LLM markdown 嗅探兜底层 — 防 LLM 把 `{"markdown": "..."}` 当字符串输出.

覆盖:
- qodercli_subprocess._unwrap_markdown_wrapper
- renderer._maybe_unwrap_llm_markdown
"""
from __future__ import annotations

import json

from reviewagent.llm.qodercli_subprocess import _unwrap_markdown_wrapper
from reviewagent.reporting.renderer import _maybe_unwrap_llm_markdown


# ---- _unwrap_markdown_wrapper (client 端) ----------------------------

def test_unwrap_literal_json_wrapper():
    text = json.dumps({"markdown": "**概述**\n\nhello"})
    assert _unwrap_markdown_wrapper(text) == "**概述**\n\nhello"


def test_unwrap_multiline_literal_json():
    """LLM 真有把字面 JSON 用多行 / pretty format 输出的."""
    text = '{\n  "markdown": "**高风险模块**\\n\\n本周 5 个 MR..."\n}'
    assert _unwrap_markdown_wrapper(text).startswith("**高风险模块**")


def test_unwrap_pure_markdown_unchanged():
    md = "**概述**\n\n本周 42 个 MR 共产生 367 条 suggestion"
    assert _unwrap_markdown_wrapper(md) == md


def test_unwrap_empty():
    assert _unwrap_markdown_wrapper("") == ""
    assert _unwrap_markdown_wrapper(None or "") == ""


def test_unwrap_dict_without_markdown_key():
    text = json.dumps({"foo": "bar"})
    assert _unwrap_markdown_wrapper(text) == text


def test_unwrap_not_starts_with_brace():
    md = "开头没有 brace 的 markdown text"
    assert _unwrap_markdown_wrapper(md) == md


def test_unwrap_malformed_json_returns_original():
    text = "{not valid json"
    assert _unwrap_markdown_wrapper(text) == text


def test_unwrap_json_array_returns_original():
    text = "[1, 2, 3]"
    assert _unwrap_markdown_wrapper(text) == text


def test_unwrap_markdown_not_string_returns_original():
    text = json.dumps({"markdown": 123})  # markdown 字段是 int
    assert _unwrap_markdown_wrapper(text) == text


# ---- _maybe_unwrap_llm_markdown (renderer 端) -----------------------

def test_renderer_unwrap():
    """renderer 入口调用 _maybe_unwrap_llm_markdown 兜底, 与 client 端等价."""
    raw = json.dumps({"markdown": "**概述**\n\nhello"})
    assert _maybe_unwrap_llm_markdown(raw) == "**概述**\n\nhello"


def test_renderer_passthrough_none():
    assert _maybe_unwrap_llm_markdown(None) == ""


def test_renderer_passthrough_pure_markdown():
    md = "**跟进建议**\n\n- 启用 ruff 加入 CI"
    assert _maybe_unwrap_llm_markdown(md) == md


# ---- 第 2 层 fallback: LLM 在 markdown value 里嵌入了未转义的 " ----

def test_unwrap_unescaped_quotes_in_markdown_value():
    """LLM 在 markdown 字符串里写了字面 `"world"` (代码 span), 未转义,
    严格 JSON parse 失败 → fallback 正则定位 wrapper, 反转义保留真实内容."""
    text = '{\n  "markdown": "返回 `\\"world\\"` 的 hello() 函数"\n}'
    out = _unwrap_markdown_wrapper(text)
    # fallback 应剥开包装, 取出 markdown value (保留字面 `"world"`)
    assert out.startswith("返回 ")
    assert '"world"' in out
    assert "hello()" in out


def test_renderer_unwrap_unescaped_quotes():
    """renderer 端的 fallback 与 client 端行为对齐."""
    text = '{\n  "markdown": "返回 `\\"world\\"` 的 hello() 函数"\n}'
    out = _maybe_unwrap_llm_markdown(text)
    assert out.startswith("返回 ")
    assert '"world"' in out


def test_unwrap_unescaped_quotes_with_escaped_newlines():
    """混合: markdown value 里有未转义 `"` 也有正确转义的 `\\n`. 反转义保留两者."""
    text = '{\n  "markdown": "**高风险模块**\\n\\n返回 `\\"foo\\"` 的 bar"\n}'
    out = _unwrap_markdown_wrapper(text)
    assert "**高风险模块**" in out
    assert "\n\n" in out
    assert '"foo"' in out
    assert "bar" in out


def test_unwrap_unescaped_quotes_actual_w32_artifact():
    """基于 W32 真实 weekly.md 里那段 literal JSON (含未转义 `"world"`),
    fallback 应该剥出真实 markdown, 长度从 2405 缩到 ~2345."""
    text = '{\n  "markdown": "**高风险模块**\\n\\n本周...smoke 名不副实\\n\\n**建议跟进**\\n\\n1. 立即排查 !226 \\n2. ...返回 `"world"` 的 hello() 函数, smoke 名不副实"}\n}'
    out = _unwrap_markdown_wrapper(text)
    # 不再以字面 `{` 开头
    assert not out.lstrip().startswith("{")
    # 内部含 `"world"` (literal) — fallback 没把这个当作 JSON 边界
    assert '"world"' in out
    # 内部含 `\\n` → 实际换行符
    assert "\n" in out


def test_unwrap_no_markdown_key_falls_through_to_regex():
    """wrapper 里没有 markdown 键时, 严格 parse 成功但取不到 markdown,
    应直接返回原 text (fallback 正则不该误命中)."""
    text = '{\n  "foo": "bar"\n}'
    assert _unwrap_markdown_wrapper(text) == text


def test_unwrap_no_closing_brace_returns_original():
    """末尾缺 `}` 时 fallback 找不到 last_brace, 应原样返回."""
    text = '{"markdown": "foo bar"'
    assert _unwrap_markdown_wrapper(text) == text
