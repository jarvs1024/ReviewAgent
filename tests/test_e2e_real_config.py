"""真实 diff 规划断言（real_config）的纯计算部分.

夹具变更与 `fixtures._CHANGE_FILES` 同源：改动夹具后这里会一起暴露出断言失效。
真实 GitLab 链路只由 `scripts/e2e/run_e2e.py --tier full` 覆盖。
"""
from __future__ import annotations

import dataclasses
import difflib

import pytest

from scripts.e2e import fixtures, real_config


@pytest.fixture(autouse=True)
def _restore_config():
    from reviewagent import config as config_mod

    saved = config_mod._current
    yield
    config_mod._current = saved


def _scenario_cfg():
    from reviewagent.config import current
    from reviewagent.scenario.profile import load_profile

    profile = load_profile(None)
    return current(), profile


def _review_cfg():
    """镜像 `config/profiles/ssd-python-autotest/review.yaml` 的当前生效值.

    单测隔离了用户 `config/` 目录，所以这里显式固定；改 review.yaml 时同步更新。
    """
    base, _profile = _scenario_cfg()
    return dataclasses.replace(
        base,
        improve_review_mode="test",
        improve_skip_test_paths=("tests/", "tmp_scripts/", "docs/", "tools/"),
        improve_keyword_paths=(
            "/testcase/", "/testlib/lib_baw/", "/testlib/lib_bmaw/", "/testlib/lib_caw/",
            "/test_components/", "/domain/", "/infrastructure/", "/framework/",
            "/conftest.py",
        ),
        improve_test_sample_paths=("testcase/",),
        improve_test_sample_max=50,
        improve_max_suggestions=20,
        improve_min_severity="medium",
        improve_full_files=5,
        improve_max_files_to_review=60,
    )


def _added_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    head = [
        f"diff --git a/{path} b/{path}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    return "\n".join(head + [f"+{line}" for line in lines]) + "\n"


def _modified_diff(path: str, base: str, new: str) -> str:
    """模拟 GitLab 对已存在文件的 diff（真实 MR 里夹具文件是修改而不是新增）."""
    hunks = list(
        difflib.unified_diff(
            base.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    head = [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
    ]
    return "".join(head + hunks[2:])


def _changes(paths: dict[str, str]) -> real_config.ChangeSet:
    files = tuple(sorted(paths))
    diffs = {}
    new_files = set()
    for fp, content in paths.items():
        base = fixtures._BASE_FILES.get(fp)
        if base is None:
            diffs[fp] = _added_diff(fp, content)
            new_files.add(fp)
        else:
            diffs[fp] = _modified_diff(fp, base, content)
    return real_config.ChangeSet(files, diffs, dict(paths), frozenset(new_files))


def test_review_config_checks_pass_on_fixture_changes() -> None:
    cfg, profile = _scenario_cfg()
    cfg = _review_cfg()
    changes = _changes(fixtures._CHANGE_FILES)
    checks = {
        name: (ok, detail)
        for name, ok, detail in real_config.audit_review_config(
            changes, cfg, profile=profile
        )
    }
    assert {name for name, (ok, _) in checks.items() if not ok} == set(), checks
    assert set(checks) == {"review_mode", "priority_paths", "full_files"}


def test_priority_paths_check_fails_without_keyword_paths() -> None:
    cfg, profile = _scenario_cfg()
    cfg = dataclasses.replace(_review_cfg(), improve_keyword_paths=())
    changes = _changes(fixtures._CHANGE_FILES)
    checks = {
        name: ok
        for name, ok, _ in real_config.audit_review_config(changes, cfg, profile=profile)
    }
    assert checks["priority_paths"] is False


def test_full_files_check_fails_when_quota_exhausted() -> None:
    cfg, profile = _scenario_cfg()
    cfg = dataclasses.replace(_review_cfg(), improve_full_files=0)
    changes = _changes(fixtures._CHANGE_FILES)
    checks = {
        name: ok
        for name, ok, _ in real_config.audit_review_config(changes, cfg, profile=profile)
    }
    assert checks["full_files"] is False


def _sampling_changes(count: int = real_config.SAMPLING_COUNT) -> real_config.ChangeSet:
    paths = {
        f"testcase/sample_{idx:02d}.py": "def test_sample():\n    assert True\n"
        for idx in range(1, count + 1)
    }
    return _changes(paths)


def test_sampling_audit_samples_real_branch_files() -> None:
    cfg, profile = _scenario_cfg()
    checks = real_config.audit_sampling(_sampling_changes(), _review_cfg(), profile=profile)
    assert len(checks) == 1
    name, ok, detail = checks[0]
    assert name == "test_sampling" and ok, detail
    assert "kept=50" in detail and "skipped=1" in detail


def test_sampling_audit_fails_when_threshold_above_file_count() -> None:
    _, profile = _scenario_cfg()
    wide = dataclasses.replace(_review_cfg(), improve_test_sample_max=60)
    (_, ok, detail), = real_config.audit_sampling(_sampling_changes(), wide, profile=profile)
    assert ok is False, detail


def test_changes_diff_headers_restored_for_line_map() -> None:
    body = "@@ -0,0 +1,2 @@\n+one\n+two\n"
    changes = real_config.ChangeSet(
        ("app/api/handler.py",),
        {"app/api/handler.py": real_config._with_header("app/api/handler.py", body)},
        {"app/api/handler.py": "one\ntwo\n"},
    )
    assert changes.diffs["app/api/handler.py"].startswith("diff --git ")
    from reviewagent.git.diff_lines import parse_diff_line_map

    assert parse_diff_line_map(changes.diffs["app/api/handler.py"]) == {
        "app/api/handler.py": {1, 2}
    }
