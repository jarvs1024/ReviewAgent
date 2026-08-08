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
