"""`review/validation.py` 校验顺序 / 合并 / 采纳判定 policy 注入.

覆盖:
* 校验顺序（文件不在 diff / snap / 多行替换 / 收缩 / 删除 / 对齐失败）
* `PythonPolicy.validate_suggestion` 薄派发与直接调用 `validate_suggestion` 一致
* `process_adopt` / `auto_detect_applied` 的 policy 关键字注入（显式 keywords 优先）
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from reviewagent.commands import suggestion_actions as sa
from reviewagent.config import current
from reviewagent.review.policies.python import PythonPolicy, find_line_by_existing_code
from reviewagent.review.types import FileContext
from reviewagent.review.validation import (
    suggestion_content_fingerprint,
    suggestion_fingerprint,
    validate_suggestion,
)
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.resolver import assemble_base

LINES = ("import os", "def f():", "    return open('x').read()")


def _ctx():
    return FileContext(file_path="core.py", lines=LINES, valid_lines=frozenset({3}),
                       diff="+    return open('x').read()")


def _run(sug, *, lines=LINES, valid=(3,), line_map=None, file_sources=None):
    """用真实 PythonPolicy 走一次 `validate_suggestion`（file_sources 预置，避免读盘）."""
    profile = load_profile(None)
    policy = PythonPolicy(profile=profile, cfg=current(), read_lines=lambda p: list(lines))
    return validate_suggestion(
        sug,
        ctx=FileContext(
            file_path="core.py", lines=tuple(lines),
            valid_lines=frozenset(valid), diff="",
        ),
        policy=policy,
        rule_set=assemble_base(profile),
        cfg=current(),
        find_line=find_line_by_existing_code,
        file_sources={"core.py": list(lines)} if file_sources is None else file_sources,
        line_map={"core.py": set(valid)} if line_map is None else line_map,
        project_id=1, mr_iid=2,
    )


# ---------- 校验顺序 ----------

def test_drop_when_file_not_in_diff():
    profile = load_profile(None)
    policy = PythonPolicy(profile=profile, cfg=current(), read_lines=lambda p: [])
    verdict = validate_suggestion(
        {"file": "other.py", "start_line": 1, "existing_code": "x", "improved_code": "y"},
        ctx=_ctx(), policy=policy, rule_set=assemble_base(profile),
        cfg=current(), find_line=lambda *a, **k: 0,
        file_sources={"core.py": list(LINES)},
        line_map={"core.py": {3}},
    )
    assert verdict["action"] == "drop"


def test_invalid_start_line_drops_without_reading_file():
    """start_line 非正整数 → 入口直接 drop, 不触发惰性读盘 (file_sources 保持空)."""
    for bad in (0, None):
        sources: dict[str, list[str]] = {}
        verdict = _run(
            {
                "file": "core.py", "start_line": bad,
                "existing_code": "    return open('x').read()",
                "improved_code": "    return open('x').read()  # noqa",
            },
            file_sources=sources,
        )
        assert verdict == {
            "action": "drop", "new_line": 0, "reason": "invalid start_line",
        }, (bad, verdict)
        assert sources == {}, bad


def test_post_snaps_to_existing_code_line_and_returns_normalised_code():
    """existing_code 反查命中 → new_line snap 到真实行, normalised_code 原样带回."""
    verdict = _run({
        "file": "core.py", "start_line": 1,  # agent 行号写错
        "existing_code": "    return open('x').read()",
        "improved_code": "    return open('x').read()  # noqa",
    })
    assert verdict["action"] == "post", verdict
    assert verdict["reason"] == "ok"
    assert verdict["new_line"] == 3
    assert verdict["normalised_code"] == "    return open('x').read()  # noqa"


def test_multi_line_replacement_posts_with_normalised_code():
    """improved 行数 > existing 行数 → multi_line_replacement (信任 existing_code 反查)."""
    verdict = _run({
        "file": "core.py", "start_line": 2,
        "existing_code": "    return open('x').read()",
        "improved_code": "    with open('x') as fh:\n        return fh.read()",
    })
    assert verdict["action"] == "post", verdict
    assert verdict["reason"] == "multi_line_replacement"
    assert verdict["new_line"] == 3
    assert verdict["normalised_code"].startswith("    with open('x') as fh:")


def test_shrink_without_new_block_opener_degrades_to_general():
    """improved 比 existing 短且无新块起始行 → 真删, 降级 general (不给 Apply)."""
    lines = (
        "import os", "def f():",
        "    a = 1", "    b = 2", "    c = 3",
        "    return a",
    )
    verdict = _run(
        {
            "file": "core.py", "start_line": 3,
            "existing_code": "    a = 1\n    b = 2\n    c = 3",
            "improved_code": "    a = 1",
        },
        lines=lines, valid=(3, 4, 5),
    )
    assert verdict["action"] == "general", verdict
    assert verdict["reason"].startswith("shrinking suggestion")


def test_empty_improved_code_with_existing_posts_as_delete_range():
    """空 improved + 非空 existing = 删除整段 → post (不能降级 general)."""
    verdict = _run({
        "file": "core.py", "start_line": 2,
        "existing_code": "    return open('x').read()",
        "improved_code": "",
    })
    assert verdict["action"] == "post", verdict
    assert verdict["new_line"] == 3


def test_alignment_mismatch_drops():
    """无 existing_code + improved 首行与目标行完全不匹配 → drop."""
    lines = ("def f():", "    x = 1")
    verdict = _run(
        {
            "file": "core.py", "start_line": 2,
            "existing_code": "",
            "improved_code": "    return 99",
        },
        lines=lines, valid=(1, 2),
    )
    assert verdict["action"] == "drop", verdict
    assert "doesn't match" in verdict["reason"]


# ---------- PythonPolicy 薄派发 / 钩子注入 ----------

def test_python_policy_dispatch_matches_direct_validation():
    """policy 薄派发与直接调用逐字段一致, 含 fix_indent 真正改写缩进的场景."""
    profile = load_profile(None)
    policy = PythonPolicy(profile=profile, cfg=current(), read_lines=lambda p: list(LINES))
    rule_set = assemble_base(profile)
    cases = [
        {  # 1: 行号写错 → snap 到 existing_code 所在行, 首行已带缩进
            "file": "core.py", "start_line": 1,
            "existing_code": "    return open('x').read()",
            "improved_code": "    return open('x').read()  # noqa",
        },
        {  # 2: target 行有缩进 / improved_code 无缩进 → 经 fix_indent 钩子补齐
            "file": "core.py", "start_line": 3,
            "existing_code": "    return open('x').read()",
            "improved_code": "return open('x').read()  # noqa",
        },
    ]
    dispatched = []
    for sug in cases:
        via_policy = policy.validate_suggestion(
            sug, _ctx(), rule_set=rule_set,
            file_sources={"core.py": list(LINES)}, line_map={"core.py": {3}},
        )
        direct = validate_suggestion(
            sug, ctx=_ctx(), policy=policy, rule_set=rule_set, cfg=current(),
            find_line=find_line_by_existing_code,
            file_sources={"core.py": list(LINES)}, line_map={"core.py": {3}},
        )
        assert via_policy == direct, sug
        dispatched.append(via_policy)

    # 场景 2 证明钩子接的是 fix_indent: 无缩进的 improved_code 被补齐为 target 的缩进
    indent_verdict = dispatched[1]
    assert indent_verdict["action"] == "post", indent_verdict
    assert indent_verdict["normalised_code"] == "    return open('x').read()  # noqa"


class _RecordingReadPolicy:
    """最小 recording wrapper: 只给 read_lines 记账, 另两个钩子直接委托."""

    def __init__(self, inner: PythonPolicy, calls: list[str]) -> None:
        self._inner = inner
        self._calls = calls

    def read_lines(self, path: str) -> list[str]:
        self._calls.append(path)
        return self._inner.read_lines(path)

    def fix_indent(self, target_line: str, improved_code: str) -> str:
        return self._inner.fix_indent(target_line, improved_code)

    def code_first_line_matches(self, target_line: str, improved_first: str) -> bool:
        return self._inner.code_first_line_matches(target_line, improved_first)


def test_lazy_read_hook_reads_once_with_suggestion_file():
    """file_sources 为空 → 经 policy.read_lines 惰性读取, 只读一次 suggestion 的文件."""
    profile = load_profile(None)
    calls: list[str] = []
    inner = PythonPolicy(profile=profile, cfg=current(), read_lines=lambda p: list(LINES))
    sources: dict[str, list[str]] = {}
    verdict = validate_suggestion(
        {
            "file": "core.py", "start_line": 3,
            "existing_code": "    return open('x').read()",
            "improved_code": "    return open('x').read()  # noqa",
        },
        ctx=_ctx(),
        policy=_RecordingReadPolicy(inner, calls),
        rule_set=assemble_base(profile),
        cfg=current(),
        find_line=find_line_by_existing_code,
        file_sources=sources,
        line_map={"core.py": {3}},
    )
    assert calls == ["core.py"]
    assert verdict["action"] == "post", verdict
    assert verdict["new_line"] == 3
    assert sources["core.py"] == list(LINES)


# ---------- 指纹门面 ----------

def test_fingerprints_moved_to_validation_module():
    from reviewagent.commands.improve import _suggestion_content_fingerprint
    from reviewagent.commands.improve import _suggestion_fingerprint as facade_fp

    assert suggestion_fingerprint("a.py", 10, "Header") == facade_fp("a.py", 10, "Header")
    assert suggestion_content_fingerprint("x = 1") == _suggestion_content_fingerprint("x = 1")
    assert len(suggestion_fingerprint("a.py", 10, "h")) == 24
    assert len(suggestion_content_fingerprint("x = 1")) == 16


# ---------- suggestion_actions policy 注入 ----------

class _StubPolicy:
    """最小策略桩: keywords() 返回哨兵关键字."""

    def keywords(self) -> set[str]:
        return {"sentinel_token"}


def test_suggestion_action_entries_accept_policy():
    assert "policy" in inspect.signature(sa.process_adopt).parameters
    assert "policy" in inspect.signature(sa.auto_detect_applied).parameters
    # 不消费关键字的入口不加死参数
    assert "policy" not in inspect.signature(sa.process_dismiss).parameters


def _fake_store(sug):
    store = MagicMock()
    store.get_suggestion_by_note_id.return_value = sug
    store.list_open_suggestions.return_value = [sug]
    store.list_resolved_suggestions.return_value = []
    return store


def _fake_gl(content):
    gl = MagicMock()
    gl.get_mr_diff_refs.return_value = {"head_sha": "newsha"}
    gl.get_file_at_sha.return_value = content
    gl.is_discussion_resolved.return_value = True
    gl.resolve_discussion.return_value = True
    gl.reply_to_discussion.return_value = 1
    gl.list_mr_commits.return_value = []
    return gl


def test_process_adopt_prefers_policy_keywords(monkeypatch):
    sug = {
        "id": 1, "note_id": "n1", "file_path": "a.py",
        "target_line": 2, "target_line_end": 2,
        "existing_code": "def foo():\n    pass\n",
        "improved_code": "def foo():\n    return sentinel_token\n",
        "head_sha": "oldsha", "state": "open",
    }
    store = _fake_store(sug)
    gl = _fake_gl("def foo():\n    pass\n")
    seen: dict[str, object] = {}

    def _spy(*args, **kwargs):
        kws = kwargs.get("keywords")
        seen["keywords"] = kws
        # 哨兵被排除 → 说明 policy 关键字集真的生效了
        return "sentinel_token" not in sa._extract_identifiers(
            kwargs["improved_code"], kws,
        )

    monkeypatch.setattr(sa, "_token_adoption_match", _spy)
    monkeypatch.setattr(sa, "GitLabClient", lambda: gl)
    monkeypatch.setattr(sa, "get_store", lambda: store)
    monkeypatch.setattr(sa, "publish_overview", lambda **kw: None)
    monkeypatch.setattr(sa, "_maybe_enqueue_reimprove", lambda **kw: None)

    result = sa.process_adopt(
        project_id=1, mr_iid=2, suggestion_note_id="n1",
        actor_username="u", reason="r", policy=_StubPolicy(),
    )
    assert result["action"] == "adopted", result
    assert seen["keywords"] == {"sentinel_token"}


def test_process_adopt_explicit_keywords_win_over_policy(monkeypatch):
    sug = {
        "id": 1, "note_id": "n1", "file_path": "a.py",
        "target_line": 2, "target_line_end": 2,
        "existing_code": "def foo():\n    pass\n",
        "improved_code": "def foo():\n    return other_token\n",
        "head_sha": "oldsha", "state": "open",
    }
    store = _fake_store(sug)
    gl = _fake_gl("def foo():\n    pass\n")
    seen: dict[str, object] = {}

    def _spy(*args, **kwargs):
        seen["keywords"] = kwargs.get("keywords")
        return True

    monkeypatch.setattr(sa, "_token_adoption_match", _spy)
    monkeypatch.setattr(sa, "GitLabClient", lambda: gl)
    monkeypatch.setattr(sa, "get_store", lambda: store)
    monkeypatch.setattr(sa, "publish_overview", lambda **kw: None)
    monkeypatch.setattr(sa, "_maybe_enqueue_reimprove", lambda **kw: None)

    result = sa.process_adopt(
        project_id=1, mr_iid=2, suggestion_note_id="n1",
        actor_username="u", reason="r",
        keywords={"explicit"}, policy=_StubPolicy(),
    )
    assert result["action"] == "adopted", result
    assert seen["keywords"] == {"explicit"}


def test_auto_detect_applied_prefers_policy_keywords(monkeypatch):
    sug = {
        "id": 1, "note_id": "n1", "file_path": "a.py",
        "target_line": 2, "target_line_end": 2,
        "existing_code": "def foo():\n    pass\n",
        "improved_code": "def foo():\n    return sentinel_token\n",
        "head_sha": "oldsha", "state": "open",
    }
    store = _fake_store(sug)
    gl = _fake_gl("def foo():\n    pass\n")
    seen: dict[str, object] = {}

    def _spy(*args, **kwargs):
        seen["keywords"] = kwargs.get("keywords")
        return True, {}

    monkeypatch.setattr(sa, "_strict_token_adoption_match", _spy)
    monkeypatch.setattr(sa, "GitLabClient", lambda: gl)
    monkeypatch.setattr(sa, "get_store", lambda: store)
    monkeypatch.setattr(sa, "publish_overview", lambda **kw: None)

    result = sa.auto_detect_applied(
        project_id=1, mr_iid=2, head_sha="newsha", policy=_StubPolicy(),
    )
    assert seen["keywords"] == {"sentinel_token"}
    assert result["applied"] == 1


def test_keywords_default_still_uses_python_set(monkeypatch):
    """keywords=None + policy=None → 保持旧行为（_KEYWORDS 默认集）."""
    assert sa._extract_identifiers("if return", None) == set()
    assert sa._resolve_keywords(None, None) is None
    assert sa._resolve_keywords(None, _StubPolicy()) == {"sentinel_token"}
