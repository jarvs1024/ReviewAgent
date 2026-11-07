"""Task 18: 增量复用 / 指纹失效 / 跨文件强制重检 / data_dir 场景隔离.

对应 spec §7.7.4 与 §7.7.5：

* `reviewed_file_state` 用真实 Store 往返（upsert → get → 喂 planner）；
* blob 未变 + 指纹一致 → 复用；`profile_fingerprint` / `ruleset_fingerprint` 变化 →
  下一轮 `files_to_review` 重新纳入（可丢弃缓存，不删旧行）+ 日志
  `review.state_invalidated reason=...`；
* 跨文件影响 → 未变文件强制重检（`cross_reuse`）；
* 无状态 → 全量重检；
* profile 与 data_dir 绑定：换 profile 必须换目录；两个 data_dir 的 telemetry
  （cohort / suggestion / reviewed_file_state）与周报目录互不可见；在途 job 的
  指纹不一致时 `verify_job` 拒绝。
"""
from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace

import pytest
from loguru import logger as loguru_logger

from reviewagent.config import current
from reviewagent.review.manifest import build_manifest, verify_job
from reviewagent.review.planner import plan
from reviewagent.scenario.profile import bind_instance, load_profile
from reviewagent.scenario.rules.resolver import assemble_base
from reviewagent.telemetry.store import Store

PROFILE_FP = "profile-fp-1"
RULESET_FP = "ruleset-fp-1"

# 与 diff 语法/语言无关的噪声词 — 让跨文件影响断言更精确
_NOISE = {"diff", "git", "index", "new", "file", "mode", "added", "line"}


class _Policy:
    """LanguagePolicy 测试替身：密度恒定 0，符号抽取用通用词法."""

    language = "python"

    def test_feature_density(self, _path: str, _size: int) -> float:
        return 0.0

    def extract_symbols(self, diff: str) -> list[str]:
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", diff or "")
        return [word for word in words if word.lower() not in _NOISE]


def _diff(file_path: str, body: str = "+value = 1") -> str:
    return (
        f"diff --git a/{file_path} b/{file_path}\n"
        "index 0000000..1111111 100644\n"
        f"--- a/{file_path}\n"
        f"+++ b/{file_path}\n"
        "@@ -0,0 +1,2 @@\n"
        f"{body}\n"
    )


def _plan(profile, diffs, *, prior_states, blobs, **kwargs):
    files = list(diffs)
    return plan(
        files,
        diffs,
        profile=profile,
        cfg=current(),
        policy=_Policy(),
        read_line_count=lambda _fp: 10,
        stat_bytes=lambda _fp: -1,
        prior_states=prior_states,
        head_sha=kwargs.pop("head_sha", "head-1"),
        blob_sha=lambda fp: blobs.get(fp),
        rule_block_bytes=0,
        project_id=34,
        mr_iid=7,
        **kwargs,
    )


@pytest.fixture
def profile():
    return load_profile(None)


@pytest.fixture
def logs():
    """捕获 loguru 消息（reviewagent.logging_setup.logger 就是同一实例）."""
    messages: list[str] = []
    handler_id = loguru_logger.add(
        lambda message: messages.append(str(message).rstrip("\n")),
        level="INFO",
        format="{message}",
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


@pytest.fixture
def file_state(tmp_path, monkeypatch):
    """真实 Store + upsert/get 往返（不写模块级单例，避免污染其他用例）."""
    store = Store(tmp_path / "telemetry.db")

    def _seed(file_paths, *, blob_sha="blob-a", head_sha="head-0",
              profile_fingerprint=PROFILE_FP, ruleset_fingerprint=RULESET_FP):
        for file_path in file_paths:
            store.upsert_file_review_state(
                project_id=34,
                mr_iid=7,
                file_path=file_path,
                blob_sha=blob_sha,
                head_sha=head_sha,
                suggestion_count=0,
                profile_fingerprint=profile_fingerprint,
                ruleset_fingerprint=ruleset_fingerprint,
            )
        return store.get_file_review_states(34, 7)

    return SimpleNamespace(store=store, seed=_seed)


# ---------------------------------------------------------------------------
# Step 2a: blob 未变 + 指纹一致 → 复用
# ---------------------------------------------------------------------------


def test_unchanged_blob_with_matching_fingerprints_is_reused(profile, file_state):
    prior = file_state.seed(["pkg/a.py"])
    assert prior["pkg/a.py"]["blob_sha"] == "blob-a"
    assert prior["pkg/a.py"]["profile_fingerprint"] == PROFILE_FP
    assert prior["pkg/a.py"]["ruleset_fingerprint"] == RULESET_FP

    result = _plan(
        profile,
        {"pkg/a.py": _diff("pkg/a.py")},
        prior_states=prior,
        blobs={"pkg/a.py": "blob-a"},
        profile_fingerprint=PROFILE_FP,
        ruleset_fingerprint=RULESET_FP,
    )

    assert "pkg/a.py" in result.reused
    assert result.files_to_review == ()


# ---------------------------------------------------------------------------
# Step 2b/2c: 指纹变化 → 下一轮重新纳入 + review.state_invalidated
# ---------------------------------------------------------------------------


def test_profile_fingerprint_change_reviews_file_again(profile, file_state, logs):
    prior = file_state.seed(["pkg/a.py"])

    result = _plan(
        profile,
        {"pkg/a.py": _diff("pkg/a.py")},
        prior_states=prior,
        blobs={"pkg/a.py": "blob-a"},
        profile_fingerprint="profile-fp-2",  # profile 已切换
        ruleset_fingerprint=RULESET_FP,
    )

    assert "pkg/a.py" in result.files_to_review
    assert result.reused == ()
    invalidated = [message for message in logs if "review.state_invalidated" in message]
    assert invalidated == [
        "review.state_invalidated project=34 mr=7 file=pkg/a.py reason=profile"
    ]
    # 「清空」= 下一轮重新纳入，而不是删除旧行（可丢弃缓存，下轮 upsert 覆盖）
    assert "pkg/a.py" in file_state.store.get_file_review_states(34, 7)


def test_ruleset_fingerprint_change_reviews_file_again(profile, file_state, logs):
    prior = file_state.seed(["pkg/a.py"])

    result = _plan(
        profile,
        {"pkg/a.py": _diff("pkg/a.py")},
        prior_states=prior,
        blobs={"pkg/a.py": "blob-a"},
        profile_fingerprint=PROFILE_FP,
        ruleset_fingerprint="ruleset-fp-2",  # RuleSet 内容变了
    )

    assert "pkg/a.py" in result.files_to_review
    assert result.reused == ()
    assert [
        message for message in logs if "review.state_invalidated" in message
    ] == ["review.state_invalidated project=34 mr=7 file=pkg/a.py reason=ruleset"]


# ---------------------------------------------------------------------------
# Step 2d: 跨文件影响 → 未变文件强制重检
# ---------------------------------------------------------------------------


def test_unchanged_file_forced_recheck_by_cross_file_impact(profile, file_state):
    diffs = {
        # a.py 未变（blob 相同），但它的符号被 b.py 的新 diff 引用
        "pkg/a.py": _diff("pkg/a.py", "+def declared_in_a(): pass"),
        "pkg/b.py": _diff("pkg/b.py", "+x = declared_in_a()"),
    }
    prior = file_state.seed(["pkg/a.py", "pkg/b.py"], blob_sha="blob-a")
    # b.py 内容变了（blob 不同），a.py 未变
    prior["pkg/b.py"]["blob_sha"] = "blob-old"

    result = _plan(
        profile,
        diffs,
        prior_states=prior,
        blobs={"pkg/a.py": "blob-a", "pkg/b.py": "blob-b"},
        profile_fingerprint=PROFILE_FP,
        ruleset_fingerprint=RULESET_FP,
    )

    assert "pkg/a.py" in result.cross_reuse
    assert "pkg/a.py" in result.files_to_review
    assert "pkg/b.py" in result.files_to_review
    assert result.reused == ()


# ---------------------------------------------------------------------------
# Step 2e: 无状态 → 全量重检
# ---------------------------------------------------------------------------


def test_missing_prior_state_reviews_everything(profile, file_state):
    assert file_state.store.get_file_review_states(34, 7) == {}

    result = _plan(
        profile,
        {"pkg/a.py": _diff("pkg/a.py"), "pkg/b.py": _diff("pkg/b.py")},
        prior_states={},
        blobs={"pkg/a.py": "blob-a", "pkg/b.py": "blob-b"},
        profile_fingerprint=PROFILE_FP,
        ruleset_fingerprint=RULESET_FP,
    )

    assert set(result.files_to_review) == {"pkg/a.py", "pkg/b.py"}
    assert result.reused == ()


# ---------------------------------------------------------------------------
# Step 4: 场景隔离（data_dir 绑定 / telemetry / 周报目录 / 在途 job）
# ---------------------------------------------------------------------------


def test_profile_scenario_isolation_across_data_dirs(tmp_path, profile):
    dir_a = tmp_path / "instance-a"
    dir_b = tmp_path / "instance-b"

    # 1) 首次绑定成功；同目录换 profile 拒绝
    bind_instance(profile, dir_a)
    with pytest.raises(RuntimeError, match="refusing"):
        bind_instance(SimpleNamespace(name="other-profile"), dir_a)

    # 2) 另一个 data_dir 可绑定另一个 profile（场景切换 = 新目录）
    bind_instance(SimpleNamespace(name="other-profile"), dir_b)

    # 3) telemetry 数据互不可见：cohort / suggestion / reviewed_file_state
    store_a = Store(dir_a / "telemetry.db")
    store_b = Store(dir_b / "telemetry.db")
    store_a.record_suggestion(
        project_id=34,
        mr_iid=7,
        note_id="note-a",
        file_path="pkg/a.py",
        target_line=12,
        existing_code="x = []",
        improved_code="x = None",
        header="可变默认参数",
        severity="high",
        head_sha="head-1",
        fingerprint="fp-a",
        cohort_key="cohort-a",
    )
    store_a.upsert_file_review_state(
        project_id=34,
        mr_iid=7,
        file_path="pkg/a.py",
        blob_sha="blob-a",
        head_sha="head-1",
        profile_fingerprint=PROFILE_FP,
        ruleset_fingerprint=RULESET_FP,
    )

    assert [row["note_id"] for row in store_a.list_latest_by_cohort(project_id=34, mr_iid=7)] == [
        "note-a"
    ]
    assert store_a.get_file_review_states(34, 7) != {}

    assert store_b.list_latest_by_cohort(project_id=34, mr_iid=7) == []
    assert store_b.get_file_review_states(34, 7) == {}
    assert store_b.suggestion_exists_by_fingerprint(34, 7, "fp-a") is False
    assert store_b.list_open_suggestions(project_id=34, mr_iid=7) == []

    # 4) 周报 store（同 data_dir 下的 weekly_reports/）不交叉
    weekly_a = replace(current(), data_dir=dir_a).weekly_reports_dir
    weekly_b = replace(current(), data_dir=dir_b).weekly_reports_dir
    assert weekly_a != weekly_b
    assert replace(current(), data_dir=dir_a).sqlite_path == dir_a / "telemetry.db"

    # 5) 在途 job 的指纹与当前环境不一致 → verify_job 拒绝（不得「尽力执行」）
    manifest = build_manifest(
        profile=profile,
        rule_set=assemble_base(profile),
        cfg=current(),
        model="DeepSeek-V4-Flash",
        provider="qodercli",
    )
    payload = {key: manifest[key] for key in (
        "profile",
        "profile_fingerprint",
        "config_fingerprint",
        "ruleset_fingerprint",
        "dynamic_rules_fingerprint",
    )}
    verify_job(payload)  # 当前 manifest 可用

    tampered = dict(payload, profile_fingerprint="stale-profile-fingerprint")
    with pytest.raises(RuntimeError, match="fingerprint"):
        verify_job(tampered)
