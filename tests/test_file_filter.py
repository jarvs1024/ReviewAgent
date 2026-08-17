"""文件筛选 / 抽样护栏单元测试 (2026-08-17).

覆盖三个纯静态 helper:
  - _new_files_from_diff: 新增文件检测
  - _sample_case_files: 确定性抽样
  - _apply_file_count_cap: 文件数硬上限 (必保新增+关键路径, tail 截断)
"""
from __future__ import annotations

from reviewagent.commands.improve import ImproveCommand


# ---------- _new_files_from_diff ----------

def test_new_files_from_diff_detects_new_and_modified(tmp_path):
    diff = tmp_path / "test.diff"
    diff.write_text(
        "diff --git a/new_foo.py b/new_foo.py\n"
        "new file mode 100644\n"
        "index 0000000..abc\n"
        "--- /dev/null\n"
        "+++ b/new_foo.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def foo():\n"
        "+    return 1\n"
        "diff --git a/modified_bar.py b/modified_bar.py\n"
        "index aaa..bbb\n"
        "--- a/modified_bar.py\n"
        "+++ b/modified_bar.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def bar():\n"
        "-    return 1\n"
        "+    return 2\n",
        encoding="utf-8",
    )
    new = ImproveCommand._new_files_from_diff(diff)
    assert new == {"new_foo.py"}
    assert "modified_bar.py" not in new


def test_new_files_from_diff_empty(tmp_path):
    diff = tmp_path / "empty.diff"
    diff.write_text("", encoding="utf-8")
    assert ImproveCommand._new_files_from_diff(diff) == set()


def test_new_files_from_dev_null_marker(tmp_path):
    """--- /dev/null 也是新增文件标志 (即使无 new file mode 行)."""
    diff = tmp_path / "devnull.diff"
    diff.write_text(
        "diff --git a/fresh.py b/fresh.py\n"
        "index 0000000..def\n"
        "--- /dev/null\n"
        "+++ b/fresh.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+x = 1\n",
        encoding="utf-8",
    )
    assert ImproveCommand._new_files_from_diff(diff) == {"fresh.py"}


# ---------- _sample_case_files ----------

def test_sample_case_files_under_max_no_sampling():
    files = ["tests/test_a.py", "src/main.py", "tests/test_b.py"]
    kept, unsampled = ImproveCommand._sample_case_files(files, ("tests/",), 10)
    assert kept == files
    assert unsampled == []


def test_sample_case_files_deterministic_and_excludes_unsampled():
    files = [
        "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
        "tests/test_d.py", "src/main.py",
    ]
    kept1, unsampled1 = ImproveCommand._sample_case_files(files, ("tests/",), 2)
    kept2, unsampled2 = ImproveCommand._sample_case_files(files, ("tests/",), 2)
    # 确定性: 同输入同输出
    assert kept1 == kept2
    assert unsampled1 == unsampled2
    # 抽样 2 个, 剔除 2 个 (4 个 case 文件)
    assert len(unsampled1) == 2
    # 非匹配文件 (src/main.py) 始终保留
    assert "src/main.py" in kept1
    # 剔除的都在 case 文件中
    for f in unsampled1:
        assert f.startswith("tests/")
    # kept + unsampled = 原始 case 文件集 + 非匹配
    assert len(kept1) == 3  # 2 sampled + src/main.py


def test_sample_case_files_empty_patterns():
    files = ["tests/test_a.py", "src/main.py"]
    kept, unsampled = ImproveCommand._sample_case_files(files, (), 5)
    assert kept == files
    assert unsampled == []


# ---------- _apply_file_count_cap ----------

def test_apply_file_count_cap_noop_under_limit():
    files = ["a.py", "b.py", "c.py"]
    kept, truncated = ImproveCommand._apply_file_count_cap(
        files, new_files=set(), keyword_paths=("/api/",), max_files=10,
    )
    assert kept == files
    assert truncated == []


def test_apply_file_count_cap_keeps_must_review_and_truncates_tail():
    # 已按优先级降序: new1(新增) > kw1(关键路径) > opt1 > opt2 > opt3
    files = ["new1.py", "api/kw1.py", "opt1.py", "opt2.py", "opt3.py"]
    new_files = {"new1.py"}
    keyword_paths = ("/api/",)
    kept, truncated = ImproveCommand._apply_file_count_cap(
        files, new_files, keyword_paths, max_files=3,
    )
    # 必保: new1 + kw1 (2 个), 预算 3-2=1, 保留 opt1, 截断 opt2/opt3
    assert "new1.py" in kept
    assert "api/kw1.py" in kept
    assert "opt1.py" in kept
    assert set(truncated) == {"opt2.py", "opt3.py"}
    assert len(kept) == 3


def test_apply_file_count_cap_must_exceeds_max_keeps_all_must():
    # 必保集合本身超过 max → 全保留 must, optional 全截断
    files = ["new1.py", "api/kw1.py", "new2.py", "opt1.py"]
    new_files = {"new1.py", "new2.py"}
    kept, truncated = ImproveCommand._apply_file_count_cap(
        files, new_files, ("/api/",), max_files=2,
    )
    # must = 3 (new1, kw1, new2) > max=2 → budget=0, optional 全截断
    assert set(kept) == {"new1.py", "api/kw1.py", "new2.py"}
    assert truncated == ["opt1.py"]


def test_apply_file_count_cap_disabled_when_zero():
    files = ["a.py"] * 100
    kept, truncated = ImproveCommand._apply_file_count_cap(
        files, set(), (), max_files=0,
    )
    assert kept == files
    assert truncated == []
