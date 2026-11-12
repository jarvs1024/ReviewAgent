"""Task 15: 运行时 manifest、RQ job 一致性校验、增量状态指纹与启动期 provider 校验.

覆盖:
* `build_manifest` 的可复现字段（服务版本 / profile / 规则 / 配置 / 动态规则）；
* `verify_job` 的四条路径：缺失字段放行（滚动升级兼容）、profile 名不一致、
  profile 指纹不一致、config / ruleset 指纹不一致；
* `reviewed_file_state` 的指纹列迁移 + upsert/get roundtrip + NULL 兼容；
* `bootstrap.startup` 对动态 provider 的启动期校验与 `BootstrapResult` 暴露.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from loguru import logger as loguru_logger

import reviewagent
from reviewagent import bootstrap
from reviewagent.config import current
from reviewagent.review.manifest import build_manifest, verify_job
from reviewagent.scenario.dynamic import load_dynamic_rules
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.prompts import split_sections
from reviewagent.scenario.rules.resolver import assemble_base
from reviewagent.settings.binder import fingerprint as binder_fingerprint
from reviewagent.settings.schema import SCHEMA
from reviewagent.telemetry.store import Store


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
def active_profile():
    return load_profile(None)


@pytest.fixture
def manifest(active_profile):
    dynamic_rules = load_dynamic_rules(active_profile)
    return build_manifest(
        profile=active_profile,
        rule_set=assemble_base(active_profile),
        cfg=current(),
        model="DeepSeek-V4-Flash",
        provider="qodercli",
        dynamic_rules=dynamic_rules,
    )


def _config_fingerprint(cfg) -> str:
    return binder_fingerprint({spec.name: getattr(cfg, spec.name) for spec in SCHEMA})


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def test_verify_job_rejects_fingerprint_mismatch():
    payload = {
        "profile": "ssd-python-autotest",
        "profile_fingerprint": "old",
        "config_fingerprint": "x",
    }
    with pytest.raises(RuntimeError, match="fingerprint"):
        verify_job(payload)


def test_manifest_contains_required_fields(manifest):
    assert set(manifest) >= {
        "service_version",
        "profile",
        "profile_fingerprint",
        "ruleset_fingerprint",
        "dynamic_rules_fingerprint",
        "config_fingerprint",
        "model",
        "provider",
    }


def test_manifest_identity_matches_active_inputs(active_profile, manifest):
    assert manifest["service_version"] == reviewagent.__version__
    assert manifest["profile"] == active_profile.name
    assert manifest["profile_fingerprint"] == active_profile.fingerprint
    assert manifest["ruleset_fingerprint"] == assemble_base(active_profile).fingerprint
    assert manifest["dynamic_rules_fingerprint"] == load_dynamic_rules(active_profile).fingerprint
    assert len(manifest["dynamic_rules_fingerprint"]) == 12
    assert (manifest["model"], manifest["provider"]) == ("DeepSeek-V4-Flash", "qodercli")


def test_manifest_config_fingerprint_equals_binder_fingerprint(manifest):
    assert manifest["config_fingerprint"] == _config_fingerprint(current())


def test_manifest_dynamic_fingerprint_empty_when_absent(active_profile):
    built = build_manifest(
        profile=active_profile,
        rule_set=assemble_base(active_profile),
        cfg=current(),
        model="m",
        provider="p",
    )
    assert built["dynamic_rules_fingerprint"] == ""


def test_manifest_accepts_current_manifest(manifest):
    # 全字段自洽 → 校验通过（不抛错）
    verify_job(dict(manifest))


def test_verify_job_missing_fields_is_accepted_with_warning(logs):
    # 滚动升级: 老 job payload 没有指纹字段, 必须继续执行.
    verify_job({"project_id": 1, "mr_iid": 2, "triggered_by": "webhook"})
    assert any("job.manifest_missing" in line for line in logs)


def test_verify_job_rejects_profile_name_mismatch(manifest):
    payload = dict(manifest, profile="other-profile")
    with pytest.raises(RuntimeError, match="job.profile_mismatch"):
        verify_job(payload)


def test_verify_job_rejects_config_fingerprint_mismatch(manifest):
    payload = dict(manifest, config_fingerprint="deadbeef")
    with pytest.raises(RuntimeError, match="fingerprint"):
        verify_job(payload)


def test_verify_job_rejects_ruleset_fingerprint_mismatch(manifest):
    payload = dict(manifest, ruleset_fingerprint="deadbeef")
    with pytest.raises(RuntimeError, match="fingerprint"):
        verify_job(payload)


# ---------------------------------------------------------------------------
# 入队指纹转发（fake Queue 捕获 q.enqueue kwargs）
# ---------------------------------------------------------------------------

_MANIFEST_KEYS = (
    "profile",
    "profile_fingerprint",
    "config_fingerprint",
    "ruleset_fingerprint",
    "dynamic_rules_fingerprint",
)


class _FakeQueue:
    """记录 `q.enqueue(func, **kwargs)` 的假队列（不连 Redis）."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue(self, func, **kwargs):
        self.calls.append({"func": func, **kwargs})
        return SimpleNamespace(id="job-1")


def _assert_flat_manifest_kwargs(call: dict) -> None:
    """断言 5 个指纹字段以扁平 kwargs 出现且取值与激活环境一致."""
    from reviewagent.scenario.profile import active_or_load
    from reviewagent.scenario.rules.resolver import active_rule_set

    assert "manifest" not in call
    for key in _MANIFEST_KEYS:
        assert key in call, f"missing flat kwarg {key}"
    profile = active_or_load()
    assert call["profile"] == profile.name
    assert call["profile_fingerprint"] == profile.fingerprint
    assert call["config_fingerprint"] == _config_fingerprint(current())
    assert call["ruleset_fingerprint"] == active_rule_set().fingerprint
    assert (
        call["dynamic_rules_fingerprint"]
        == load_dynamic_rules(profile).fingerprint
    )


@pytest.mark.parametrize(
    ("enqueue_name", "target_func"),
    [
        ("describe", "reviewagent.workers.tasks.run_describe"),
        ("improve", "reviewagent.workers.tasks.run_improve"),
    ],
)
def test_enqueue_forwards_manifest_fingerprints(monkeypatch, enqueue_name, target_func):
    from reviewagent.workers import tasks

    fake = _FakeQueue()
    monkeypatch.setattr(tasks, "get_queue", lambda: fake)

    job_id = tasks._enqueue(
        enqueue_name,
        project_id=1,
        mr_iid=2,
        triggered_by="webhook",
        actor_username="tester",
    )

    assert job_id == "job-1"
    assert fake.calls[0]["func"] == target_func
    _assert_flat_manifest_kwargs(fake.calls[0])


def test_enqueue_mr_chain_forwards_manifest_fingerprints(monkeypatch):
    from reviewagent.workers import tasks

    fake = _FakeQueue()
    monkeypatch.setattr(tasks, "get_queue", lambda: fake)

    job_ids = tasks.enqueue_mr_chain(
        commands=("describe", "improve"),
        project_id=1,
        mr_iid=2,
        triggered_by="webhook",
        actor_username="tester",
    )

    assert job_ids == ["job-1"]
    call = fake.calls[0]
    assert call["func"] == "reviewagent.workers.tasks.run_mr_chain"
    assert call["commands"] == ["describe", "improve"]
    _assert_flat_manifest_kwargs(call)


# ---------------------------------------------------------------------------
# 增量状态指纹（reviewed_file_state）
# ---------------------------------------------------------------------------

_OLD_FILE_STATE_DDL = """
CREATE TABLE reviewed_file_state (
    project_id      INTEGER NOT NULL,
    mr_iid          INTEGER NOT NULL,
    file_path       TEXT NOT NULL,
    blob_sha        TEXT,
    head_sha        TEXT,
    suggestion_count INTEGER DEFAULT 0,
    reviewed_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (project_id, mr_iid, file_path)
)
"""


def _create_old_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_OLD_FILE_STATE_DDL)
        conn.execute(
            "INSERT INTO reviewed_file_state "
            "(project_id, mr_iid, file_path, blob_sha, head_sha, suggestion_count, reviewed_at) "
            "VALUES (1, 2, 'legacy.py', 'blob', 'head', 1, '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()


def test_store_migration_adds_fingerprint_columns(tmp_path):
    path = tmp_path / "old.db"
    _create_old_db(path)

    Store(path)

    conn = sqlite3.connect(str(path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(reviewed_file_state)")}
    finally:
        conn.close()
    assert {"profile_fingerprint", "ruleset_fingerprint"} <= columns


def test_store_upsert_and_get_roundtrip_includes_fingerprints(tmp_path):
    store = Store(tmp_path / "state.db")
    store.upsert_file_review_state(
        project_id=1, mr_iid=2, file_path="a.py",
        blob_sha="blob-1", head_sha="head-1", suggestion_count=2,
        profile_fingerprint="profile-fp", ruleset_fingerprint="ruleset-fp",
    )

    states = store.get_file_review_states(1, 2)
    assert states["a.py"]["blob_sha"] == "blob-1"
    assert states["a.py"]["profile_fingerprint"] == "profile-fp"
    assert states["a.py"]["ruleset_fingerprint"] == "ruleset-fp"


def test_store_upsert_updates_fingerprints(tmp_path):
    store = Store(tmp_path / "state.db")
    kwargs = dict(project_id=1, mr_iid=2, file_path="a.py", head_sha="head-1")
    store.upsert_file_review_state(
        blob_sha="blob-1", profile_fingerprint="p1", ruleset_fingerprint="r1", **kwargs
    )
    store.upsert_file_review_state(
        blob_sha="blob-2", profile_fingerprint="p2", ruleset_fingerprint="r2", **kwargs
    )

    state = store.get_file_review_states(1, 2)["a.py"]
    assert state["blob_sha"] == "blob-2"
    assert (state["profile_fingerprint"], state["ruleset_fingerprint"]) == ("p2", "r2")


def test_store_rows_without_fingerprints_read_back_as_none(tmp_path):
    store = Store(tmp_path / "state.db")
    store.upsert_file_review_state(
        project_id=1, mr_iid=2, file_path="a.py", blob_sha="blob", head_sha="head",
    )

    state = store.get_file_review_states(1, 2)["a.py"]
    assert state["profile_fingerprint"] is None
    assert state["ruleset_fingerprint"] is None


def test_migrated_old_row_reads_back_null_fingerprints(tmp_path):
    path = tmp_path / "old.db"
    _create_old_db(path)
    store = Store(path)

    state = store.get_file_review_states(1, 2)["legacy.py"]
    assert state["blob_sha"] == "blob"
    assert state["profile_fingerprint"] is None
    assert state["ruleset_fingerprint"] is None


# ---------------------------------------------------------------------------
# 启动期 provider 校验
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch):
    """把 profiles/ 复制到 tmp 并指向独立 data_dir；启动缓存前后清理."""
    import reviewagent.config as config_module

    root = tmp_path / "profiles"
    shutil.copytree(Path(current().review_profile_root), root)
    monkeypatch.setattr(
        config_module,
        "_current",
        replace(
            config_module.current(),
            data_dir=tmp_path / "data",
            review_profile="ssd-python-autotest",
            review_profile_root=str(root),
        ),
    )
    bootstrap.reset_for_tests()
    yield root
    bootstrap.reset_for_tests()


def test_startup_exposes_dynamic_rules_and_fingerprint(isolated_profiles):
    result = bootstrap.startup("api")

    assert result.dynamic_rules is not None
    assert [rule.key for rule in result.dynamic_rules.rules] == ["DYN-CROSS-FILE-REFS"]
    assert result.dynamic_rules_fingerprint
    assert len(result.dynamic_rules_fingerprint) == 12


def test_startup_fails_on_unknown_dynamic_provider(isolated_profiles):
    dyn_path = isolated_profiles / "ssd-python-autotest" / "rules" / "dynamic_rules.yaml"
    data = yaml.safe_load(dyn_path.read_text(encoding="utf-8"))
    data["rules"][0]["provider"] = "not_registered"
    dyn_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown dynamic provider: not_registered"):
        bootstrap.startup("api")
    assert bootstrap._state is None  # 启动失败不得留下半成品结果


def test_startup_rejects_unknown_provider_even_when_disabled(isolated_profiles):
    dyn_path = isolated_profiles / "ssd-python-autotest" / "rules" / "dynamic_rules.yaml"
    data = yaml.safe_load(dyn_path.read_text(encoding="utf-8"))
    data["rules"][0]["provider"] = "not_registered"
    data["rules"][0]["enabled"] = False
    dyn_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown dynamic provider: not_registered"):
        bootstrap.startup("api")


def test_default_profile_dynamic_rules_load_with_templates(tmp_path):
    """R5: 模板 fail-fast 后默认 profile 仍可加载（两个模板文件 / section 都存在）."""
    profile = load_profile(None)
    ruleset = load_dynamic_rules(profile)
    rule = ruleset.rules[0]
    root = Path(profile.root)
    for rel in (rule.empty_template, rule.items_template):
        path_part, _, section = rel.partition("#")
        path = root / path_part
        assert path.is_file()
        if section:
            assert section in split_sections(path.read_text(encoding="utf-8"))
    raw = (root / str(profile.dynamic_rules)).read_bytes()
    assert ruleset.fingerprint == hashlib.sha256(raw).hexdigest()[:12]
