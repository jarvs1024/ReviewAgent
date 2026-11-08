"""三个配置项的纯函数 witness：并发数 / diff 超限切分 / opencode 截断."""
from __future__ import annotations

from types import SimpleNamespace

from reviewagent.commands._common import partition_overflow
from reviewagent.opencode.client import _clip_text
from reviewagent.review.runner import worker_count


def test_worker_count_bounded_by_groups_and_setting() -> None:
    cfg = SimpleNamespace(improve_parallel_workers=3)
    assert worker_count([["a"], ["b"], ["c"], ["d"], ["e"]], cfg) == 3
    assert worker_count([["a"], ["b"]], cfg) == 2
    assert worker_count([["a"], ["b"]], SimpleNamespace(improve_parallel_workers=1)) == 1


def test_partition_overflow_keeps_first_file_and_splits_tail() -> None:
    diffs = {"a.py": "x" * 80, "b.py": "y" * 80, "c.py": "z" * 80}
    scope, overflow = partition_overflow(diffs, 100)
    assert scope == ["a.py"]
    assert overflow == ["b.py", "c.py"]


def test_partition_overflow_single_big_file_stays_in_scope() -> None:
    scope, overflow = partition_overflow({"a.py": "x" * 500}, 100)
    assert scope == ["a.py"]
    assert overflow == []


def test_partition_overflow_without_limit_keeps_everything() -> None:
    diffs = {"a.py": "x" * 500, "b.py": "y" * 500}
    scope, overflow = partition_overflow(diffs, 0)
    assert scope == ["a.py", "b.py"]
    assert overflow == []


def test_clip_text_marks_original_length() -> None:
    clipped = _clip_text("abcdef", 3)
    assert clipped.startswith("abc")
    assert "原始 6 字符" in clipped
    assert _clip_text("abc", 3) == "abc"
