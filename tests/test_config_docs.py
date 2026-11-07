"""T20: `.env.example` 与 `docs/CONFIG.md` 的文档契约.

* `.env.example` 收敛为「实例平面」：每个键都必须是 `SCHEMA` 的 env（或别名），
  且对应字段 scope 必须是 `instance`；必填项与关键实例键存在，值只能是占位符。
* `docs/CONFIG.md` 由 `reviewagent config show --diff-default --sources` 生成，
  必须解释 `profile_fingerprint` 与 `config_fingerprint`，且不泄露任何凭证。
"""
from __future__ import annotations

import re
from pathlib import Path

from reviewagent.cli import main
from reviewagent.settings.schema import SCHEMA, SCHEMA_BY_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CONFIG_DOC = REPO_ROOT / "docs" / "CONFIG.md"

_KEY_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
_OUTPUT_HEADING = "## 生成输出"

_ENV_INDEX = {}
for _spec in SCHEMA:
    for _key in (_spec.env, *_spec.aliases):
        if _key:
            _ENV_INDEX[_key] = _spec


def _env_example_assignments() -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = _KEY_RE.match(line.strip())
        if match:
            assignments[match.group("key")] = match.group("value").strip()
    return assignments


def test_env_example_is_instance_plane_only():
    assignments = _env_example_assignments()
    assert assignments, ".env.example has no assignments"
    unknown = sorted(key for key in assignments if key not in _ENV_INDEX)
    assert not unknown, f"keys outside SCHEMA env set: {unknown}"
    non_instance = sorted(
        key for key in assignments if _ENV_INDEX[key].scope != "instance"
    )
    assert not non_instance, f"scenario-plane keys must not live in .env.example: {non_instance}"


def test_env_example_keeps_required_and_instance_keys():
    assignments = _env_example_assignments()
    required = {spec.env for spec in SCHEMA if spec.name in ("gitlab_url", "gitlab_pat", "gitlab_webhook_secret")}
    assert required <= set(assignments)
    for key in (
        "GITLAB_BOT_USERNAME",
        "LLM_PROVIDER",
        "QODER_SDK_PAT",
        "QODER_SDK_MODEL",
        "REDIS_URL",
        "RQ_WORKER_COUNT",
        "RQ_WORKER_TIMEOUT",
        "REVIEW_PROFILE",
        "REVIEWAGENT_DATA_DIR",
        "REVIEWAGENT_LOG_LEVEL",
        "LOG_FORMAT",
        "REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL",
        "REVIEWAGENT_WEEKLY_DINGTALK_SECRET",
    ):
        assert key in assignments, key


def test_env_example_secrets_are_placeholders_only():
    assignments = _env_example_assignments()
    for key in ("GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_WEBHOOK_SECRET",
                "QODER_SDK_PAT", "REVIEWAGENT_WEEKLY_DINGTALK_SECRET"):
        assert assignments[key] == "", key
    assert assignments["GITLAB_URL"] == ""


def test_env_example_has_no_duplicate_keys():
    keys = [
        match.group("key")
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if (match := _KEY_RE.match(line.strip()))
    ]
    assert len(keys) == len(set(keys))


def test_config_doc_explains_both_fingerprints():
    text = CONFIG_DOC.read_text(encoding="utf-8")
    assert "profile_fingerprint" in text
    assert "config_fingerprint" in text
    assert "SHA-256" in text
    # 生成时必须使用 stub 凭证：任何真实/临时值都不得落盘
    assert "stub-pat" not in text
    assert "stub-secret" not in text
    assert "test-token" not in text


def test_config_doc_output_section_is_generated_from_cli(tmp_path, monkeypatch, capsys):
    """生成段落必须与当前 CLI 输出一致（防止文档与 schema 漂移）."""
    from reviewagent.settings.schema import SCHEMA as _SCHEMA

    for spec in _SCHEMA:  # 清掉开发者环境里的干扰变量，保证输出只由本测试决定
        for key in (spec.env, *spec.aliases):
            if key:
                monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.invalid")
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "stub-pat")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "stub-secret")
    monkeypatch.setenv("REVIEW_PROFILE", "ssd-python-autotest")
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path / "data"))

    assert main(["config", "show", "--diff-default", "--sources"]) == 0
    actual = set(capsys.readouterr().out.splitlines())

    text = CONFIG_DOC.read_text(encoding="utf-8")
    assert _OUTPUT_HEADING in text
    documented = [
        line for line in text.split(_OUTPUT_HEADING, 1)[1].splitlines() if line.strip()
    ]
    assert documented, "CONFIG.md has no generated output"
    missing = [line for line in documented if line not in actual]
    assert not missing, f"CONFIG.md is stale, regenerate it: {missing}"
    for line in documented:
        name = line.split(":", 1)[0]
        assert name in SCHEMA_BY_NAME, f"documented field no longer in SCHEMA: {name}"
