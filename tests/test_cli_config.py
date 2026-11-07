"""CLI `config check/show` 与 bootstrap 启动装配（Task 3）."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from reviewagent import bootstrap
from reviewagent.cli import main
from reviewagent.scenario import profile as profile_module

# ---------------- config check ----------------


def test_config_check_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    assert main(["config", "check", "--profile", "ssd-python-autotest"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ok")
    assert "profile: ssd-python-autotest" in out
    assert "name: ssd-python-autotest" in out
    assert "fingerprint: " in out
    assert f"data_dir: {tmp_path}" in out
    # check 同时完成实例绑定，systemd ExecStartPre 之后启动即可直接复用
    assert json.loads((tmp_path / "instance.json").read_text())["profile"] == "ssd-python-autotest"


def test_config_check_unknown_profile_lists_available(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    assert main(["config", "check", "--profile", "nope"]) == 2
    err = capsys.readouterr().err
    assert "ssd-python-autotest" in err
    assert err.startswith("config check failed: ")
    assert "Traceback" not in err


def test_config_check_missing_required_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GITLAB_PERSONAL_ACCESS_TOKEN", raising=False)
    assert main(["config", "check"]) == 2
    err = capsys.readouterr().err
    assert "missing required env var: GITLAB_PERSONAL_ACCESS_TOKEN" in err
    assert "Traceback" not in err


def test_config_check_instance_conflict(tmp_path, monkeypatch, capsys):
    (tmp_path / "instance.json").write_text(
        json.dumps({"schema_version": 1, "profile": "other"}), encoding="utf-8"
    )
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    assert main(["config", "check", "--profile", "ssd-python-autotest"]) == 2
    err = capsys.readouterr().err
    assert "is bound to profile 'other'" in err
    assert "Traceback" not in err


# ---------------- config show ----------------


def test_config_show_redacts_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "super-secret")
    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "super-secret" not in out
    assert "gitlab_pat: ***" in out
    assert "***" in out


def test_config_show_masks_url_credentials_and_query_tokens(tmp_path, monkeypatch, capsys):
    """URL 内嵌密码与 query 凭证都必须脱敏（redis / 钉钉 webhook）."""
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REDIS_URL", "redis://user:pass@127.0.0.1:6379/1")
    monkeypatch.setenv(
        "REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL",
        "https://oapi.dingtalk.com/robot/send?access_token=tok123",
    )

    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out

    # 密码原值不得出现（字段名 `opencode_password` 含 "pass"，故按完整片段断言）。
    assert "user:pass" not in out
    assert ":pass@" not in out
    assert "tok123" not in out
    assert "redis_url: redis://user:***@127.0.0.1:6379/1" in out
    assert "access_token=***" in out


def test_config_show_masks_query_tokens_without_scheme(tmp_path, monkeypatch, capsys):
    """少写 scheme 的 webhook 地址同样不得泄露 access_token."""
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL",
        "oapi.dingtalk.com/robot/send?access_token=tok456",
    )

    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out

    assert "tok456" not in out
    assert "oapi.dingtalk.com/robot/send?access_token=***" in out


def test_config_show_keeps_url_without_credentials_intact(tmp_path, monkeypatch, capsys):
    """无敏感成分的 URL（默认 redis）不得被改写."""
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL", raising=False)

    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out

    assert "redis_url: redis://localhost:6379/0" in out


def test_config_show_sources(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IMPROVE_REVIEW_MODE", "test")
    assert main(["config", "show", "--sources"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    by_key = {line.split(":", 1)[0]: line for line in lines}

    # 稳定排序：字段名升序（输出可 diff）
    assert [line.split(":", 1)[0] for line in lines] == sorted(by_key)
    # env 显式设置 → 值取自本次 env 解析，来源标 env
    assert by_key["improve_review_mode"] == "improve_review_mode: test  # source=env"
    # 只出现在 preset 里的映射字段
    assert by_key["improve_max_suggestions"] == "improve_max_suggestions: 15  # source=preset"
    # profile.yaml 自身写的字段
    assert by_key["rule_key_prefix"] == "rule_key_prefix: SSD  # source=profile"
    # 未出现在任何 profile/env 的字段
    assert by_key["log_level"] == "log_level: INFO  # source=default"


def test_config_show_diff_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IMPROVE_MAX_SUGGESTIONS", "7")
    assert main(["config", "show", "--diff-default"]) == 0
    out = capsys.readouterr().out
    assert "improve_max_suggestions: 7" in out
    assert f"data_dir: {tmp_path}" in out  # data_dir 也不是默认值
    assert "log_level" not in out  # 与默认一致 → 不显示
    assert "improve_max_suggestions_per_file" not in out


# ---------------- bootstrap ----------------


class _LogRecorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message: str, *args) -> None:
        self.lines.append(message.format(*args))


@pytest.fixture
def isolated_instance(tmp_path, monkeypatch):
    """把 `current()` 指向 tmp data_dir + 固定 profile，并清空启动缓存."""
    import reviewagent.config as config_module

    monkeypatch.setattr(
        config_module,
        "_current",
        replace(
            config_module.current(),
            data_dir=tmp_path,
            review_profile="ssd-python-autotest",
        ),
    )
    bootstrap.reset_for_tests()
    yield tmp_path
    bootstrap.reset_for_tests()


def test_startup_is_idempotent(isolated_instance):
    first = bootstrap.startup("api")
    second = bootstrap.startup("api")
    assert second is first
    assert (first.kind, first.profile_name) == ("api", "ssd-python-autotest")
    assert first.profile_fingerprint and first.config_fingerprint
    assert first.data_dir == isolated_instance
    assert first.rule_set is not None
    assert "R-LOG" in first.rule_set.index_by_key
    assert json.loads((isolated_instance / "instance.json").read_text())["profile"] == (
        "ssd-python-autotest"
    )
    assert bootstrap.readiness() == (True, [])


def test_startup_logs_config_resolved_and_bootstrap_ready_once(isolated_instance, monkeypatch):
    recorder = _LogRecorder()
    monkeypatch.setattr(bootstrap, "logger", SimpleNamespace(info=recorder.info))

    bootstrap.startup("worker")
    bootstrap.startup("worker")

    resolved = [line for line in recorder.lines if line.startswith("config.resolved")]
    ready = [line for line in recorder.lines if line.startswith("bootstrap.ready")]
    assert len(resolved) == 1
    assert len(ready) == 1
    assert "kind=worker" in ready[0]
    assert "profile=ssd-python-autotest" in ready[0]
    assert "profile_fingerprint=" in ready[0]
    assert "config_fingerprint=" in ready[0]
    assert "rule_fingerprint=" in ready[0]
    assert f"data_dir={isolated_instance}" in ready[0]


def test_readiness_before_startup_reports_reasons(isolated_instance):
    ok, reasons = bootstrap.readiness()
    assert ok is False
    joined = "\n".join(reasons)
    assert "not initialized" in joined  # profile 未激活
    assert "not bound" in joined  # data_dir 未绑定


def test_readiness_reports_unbound_data_dir(isolated_instance):
    profile_module.load_and_activate()
    ok, reasons = bootstrap.readiness()
    assert ok is False
    assert any("not bound" in reason for reason in reasons)


def test_readiness_reports_invalid_config(isolated_instance, monkeypatch):
    import reviewagent.config as config_module

    monkeypatch.setattr(
        config_module,
        "_current",
        replace(config_module.current(), data_dir=isolated_instance, gitlab_url=""),
    )
    ok, reasons = bootstrap.readiness()
    assert ok is False
    assert any("missing required config: gitlab_url" in reason for reason in reasons)


def test_readiness_check_hook_is_replaceable_and_reset(isolated_instance):
    bootstrap.startup("api")
    seen: list[str] = []

    def redis_probe() -> list[str]:
        seen.append("called")
        return ["redis unavailable (stub)"]

    bootstrap.register_readiness_check(redis_probe)
    ok, reasons = bootstrap.readiness()
    assert ok is False
    assert "redis unavailable (stub)" in reasons
    assert seen

    bootstrap.reset_for_tests()  # 恢复默认钩子 + 清空激活状态
    ok, reasons = bootstrap.readiness()
    assert ok is False  # reset 后 profile 不再激活
    assert not any("redis unavailable" in reason for reason in reasons)


def test_reset_for_tests_clears_activation_and_startup_state(isolated_instance):
    bootstrap.startup("api")
    assert bootstrap.readiness()[0] is True

    bootstrap.reset_for_tests()

    with pytest.raises(RuntimeError, match="not initialized"):
        profile_module.active()
    assert bootstrap.readiness()[0] is False
    # 再次 startup 会重新装配（不是返回缓存）并仍然成功
    assert bootstrap.startup("api").profile_name == "ssd-python-autotest"


# ---------------- 启动接线（API lifespan / worker） ----------------


def test_api_lifespan_runs_bootstrap(isolated_instance):
    from fastapi.testclient import TestClient

    from reviewagent import main as main_module

    with TestClient(main_module.app) as client:
        assert bootstrap.readiness()[0] is True
        assert client.get("/").status_code == 200


def test_worker_bootstrap_runs_startup_before_loop(isolated_instance, monkeypatch):
    from reviewagent.workers import rq_worker

    calls: list[str] = []
    monkeypatch.setattr(
        rq_worker.SpawnWorker,
        "bootstrap",
        lambda self, **kwargs: calls.append("super.bootstrap") or "super",
    )
    worker = object.__new__(rq_worker.ReviewAgentSpawnWorker)
    assert worker.bootstrap() == "super"
    assert calls == ["super.bootstrap"]
    assert bootstrap.readiness()[0] is True


def test_api_lifespan_aborts_on_invalid_profile(tmp_path, monkeypatch):
    """REVIEW_PROFILE 非法时 lifespan 启动即失败（uvicorn 会据此拒绝启动）."""
    from fastapi.testclient import TestClient

    import reviewagent.config as config_module
    from reviewagent import main as main_module
    from reviewagent.settings import binder

    monkeypatch.setenv("REVIEW_PROFILE", "nope")
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    # 模拟真实启动：按当前 env 重新解析配置（含 review_profile / data_dir）。
    monkeypatch.setattr(config_module, "_current", binder.from_env())
    # 模块级 `_state` 缓存会掩盖失败，前后都要清掉。
    bootstrap.reset_for_tests()
    try:
        with pytest.raises(ValueError, match="profile 'nope' not found"):
            with TestClient(main_module.app):
                pass  # pragma: no cover — 启动必须失败，进不到用例体
    finally:
        bootstrap.reset_for_tests()


def test_worker_bootstrap_propagates_startup_failure(monkeypatch):
    """启动装配失败必须从 worker bootstrap 冒泡（不能带错场景继续跑任务）."""
    from reviewagent.workers import rq_worker

    def boom(kind: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(bootstrap, "startup", boom)
    bootstrap.reset_for_tests()
    try:
        worker = object.__new__(rq_worker.ReviewAgentSpawnWorker)
        with pytest.raises(RuntimeError, match="boom"):
            worker.bootstrap()
    finally:
        bootstrap.reset_for_tests()
