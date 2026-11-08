"""CLI `profile list` / `profile switch`：场景包查看与切换（含 env 文件改写）."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from reviewagent.cli import main


def _write_profile(root: Path, name: str, description: str | None = None) -> None:
    data = {
        "schema_version": 1,
        "name": name,
        "language": "python",
        "prompts": {"improve": "prompts/improve.md", "describe": "prompts/describe.md"},
    }
    if description:
        data["description"] = description
    profile_dir = root / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    for rel in data["prompts"].values():
        path = profile_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n", encoding="utf-8")


def _write_env_file(path: Path, extra: dict[str, str] | None = None) -> None:
    content = [
        "# instance env",
        "REVIEW_PROFILE=old",
        "REVIEWAGENT_DATA_DIR=data/old",
    ]
    for key, value in (extra or {}).items():
        content.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(content) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


# ---------------- profile list ----------------


def test_profile_list_marks_current(tmp_path, monkeypatch, capsys):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "base")
    _write_profile(profiles, "alt", description="备选场景")
    # current 取自 env 文件（重启后新进程读到的就是它）
    env_file = tmp_path / "instance.env"
    env_file.write_text("REVIEW_PROFILE=alt\n", encoding="utf-8")
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(env_file))

    assert main(["profile", "list"]) == 0
    out = capsys.readouterr().out.splitlines()

    assert "* alt (备选场景)" in out
    assert "  base" in out
    assert out[-1] == "(current: alt; switch with: reviewagent profile switch <name>)"


def test_profile_list_env_file_absent_uses_process_env(tmp_path, monkeypatch, capsys):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "ssd-python-autotest")
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEW_PROFILE", "ssd-python-autotest")
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(tmp_path / "absent.env"))

    assert main(["profile", "list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "* ssd-python-autotest" in out


def test_profile_list_defaults_current_to_default_profile(tmp_path, monkeypatch, capsys):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "ssd-python-autotest")
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.delenv("REVIEW_PROFILE", raising=False)
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(tmp_path / "absent.env"))

    assert main(["profile", "list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "* ssd-python-autotest" in out


def test_profile_list_empty_root_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(tmp_path / "empty"))

    assert main(["profile", "list"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("no profiles found under ")
    assert "Traceback" not in err


# ---------------- profile switch ----------------


def test_profile_switch_rewrites_env_file(tmp_path, monkeypatch, capsys):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "old")
    _write_profile(profiles, "alt")
    env_file = tmp_path / "instance.env"
    _write_env_file(env_file, {"REVIEWAGENT_DATA_DIR": str(tmp_path / "data" / "old")})
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(env_file))

    assert main(["profile", "switch", "alt"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "profile: alt" in out
    # 绝对 data_dir → 同级兄弟目录
    assert f"data_dir: {tmp_path / 'data' / 'alt'}" in out
    assert f"env_file: {env_file}" in out
    assert any(line.startswith("restart: ") for line in out)

    values = _env_values(env_file)
    assert values["REVIEW_PROFILE"] == "alt"
    assert values["REVIEWAGENT_DATA_DIR"] == str(tmp_path / "data" / "alt")
    # 注释保留 + 权限不变
    text = env_file.read_text(encoding="utf-8")
    assert "# instance env" in text
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_profile_switch_relative_data_dir_uses_data_prefix(tmp_path, monkeypatch, capsys):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "alt")
    env_file = tmp_path / "instance.env"
    _write_env_file(env_file)  # REVIEWAGENT_DATA_DIR=data/old（相对）
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(env_file))

    assert main(["profile", "switch", "alt"]) == 0
    values = _env_values(env_file)
    assert values["REVIEWAGENT_DATA_DIR"] == "data/alt"


def test_profile_switch_appends_missing_keys(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "alt")
    env_file = tmp_path / "instance.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("# only a comment\n", encoding="utf-8")
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(env_file))

    assert main(["profile", "switch", "alt"]) == 0
    values = _env_values(env_file)
    assert values["REVIEW_PROFILE"] == "alt"
    assert values["REVIEWAGENT_DATA_DIR"] == "data/alt"


def test_profile_switch_missing_profile_fails_and_keeps_file(tmp_path, monkeypatch, capsys):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "alt")
    env_file = tmp_path / "instance.env"
    _write_env_file(env_file)
    before = env_file.read_text(encoding="utf-8")
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(env_file))

    assert main(["profile", "switch", "nope"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("profile switch failed: ")
    assert "nope" in err
    assert "Traceback" not in err
    assert env_file.read_text(encoding="utf-8") == before


def test_profile_switch_refuses_data_dir_bound_to_other_profile(
    tmp_path, monkeypatch, capsys
):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "alt")
    data_dir = tmp_path / "data" / "alt"
    data_dir.mkdir(parents=True)
    (data_dir / "instance.json").write_text(
        json.dumps({"schema_version": 1, "profile": "other"}), encoding="utf-8"
    )
    env_file = tmp_path / "instance.env"
    _write_env_file(env_file, {"REVIEWAGENT_DATA_DIR": str(data_dir)})
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(env_file))

    assert main(["profile", "switch", "alt"]) == 2
    err = capsys.readouterr().err
    assert "bound to profile 'other'" in err
    assert _env_values(env_file)["REVIEW_PROFILE"] == "old"


def test_profile_switch_same_profile_keeps_data_dir(tmp_path, monkeypatch, capsys):
    """切到当前场景是幂等操作：数据目录保持原值，不被推导改写."""
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "alt")
    env_file = tmp_path / "instance.env"
    # REVIEW_PROFILE=alt 且未写 REVIEWAGENT_DATA_DIR（schema 默认 ./data）
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "# instance env\nREVIEW_PROFILE=alt\n", encoding="utf-8"
    )
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(env_file))

    assert main(["profile", "switch", "alt"]) == 0
    text = env_file.read_text(encoding="utf-8")
    assert "REVIEWAGENT_DATA_DIR" not in text
    assert _env_values(env_file)["REVIEW_PROFILE"] == "alt"


def test_profile_switch_reuses_bound_same_profile_data_dir(tmp_path, monkeypatch, capsys):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "alt")
    data_dir = tmp_path / "data" / "alt"
    data_dir.mkdir(parents=True)
    (data_dir / "instance.json").write_text(
        json.dumps({"schema_version": 1, "profile": "alt"}), encoding="utf-8"
    )
    env_file = tmp_path / "instance.env"
    _write_env_file(env_file, {"REVIEWAGENT_DATA_DIR": str(data_dir)})
    monkeypatch.setenv("REVIEW_PROFILE_ROOT", str(profiles))
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(env_file))

    assert main(["profile", "switch", "alt"]) == 0


def test_profile_switch_missing_env_file_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_ENV_FILE", str(tmp_path / "nope.env"))

    assert main(["profile", "switch", "alt"]) == 2
    err = capsys.readouterr().err
    assert "instance env file not found" in err


def test_profile_commands_work_in_bare_shell(tmp_path):
    """bare shell（无任何 ReviewAgent env）下 list/switch 必须能跑：
    reviewagent.config 在 import 期解析必填 env，source env 文件必须先于 import."""
    import os
    import subprocess
    import sys

    profiles = tmp_path / "profiles"
    _write_profile(profiles, "old")
    _write_profile(profiles, "alt")
    env_file = tmp_path / "instance.env"
    _write_env_file(env_file, {"GITLAB_URL": "https://gitlab.example.test",
                               "GITLAB_PERSONAL_ACCESS_TOKEN": "t",
                               "GITLAB_WEBHOOK_SECRET": "s",
                               "REVIEW_PROFILE": "old"})
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GITLAB_", "REVIEW", "QODER", "REDIS_", "RQ_", "IMPROVE"))
    }
    clean_env.update(
        REVIEW_PROFILE_ROOT=str(profiles),
        REVIEWAGENT_ENV_FILE=str(env_file),
    )

    list_run = subprocess.run(
        [sys.executable, "-m", "reviewagent", "profile", "list"],
        env=clean_env, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert list_run.returncode == 0, list_run.stderr
    assert "* old" in list_run.stdout

    switch_run = subprocess.run(
        [sys.executable, "-m", "reviewagent", "profile", "switch", "alt"],
        env=clean_env, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert switch_run.returncode == 0, switch_run.stderr
    assert _env_values(env_file)["REVIEW_PROFILE"] == "alt"
