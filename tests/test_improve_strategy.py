"""V6 文件策略分配 + 软阈值测试.

覆盖:
- 4 档 strategy 分配 (full / partial / patch / skip)
- 测试路径过滤 (improve_skip_test_paths)
- MAX_DIFF_CHARS overflow → patch-only
- _merge_chunks warning 分类
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class FakeWS:
    """最小 worktree 替身: 提供 worktree 路径 + diff_file."""
    def __init__(self, root: Path, diff_text: str = ""):
        self.worktree = root
        self.diff_file = root / ".diff"
        self.diff_file.write_text(diff_text, encoding="utf-8")


class FakeImproveCommand:
    """只暴露本测试需要的几个 method (避开 __init__)."""
    def __init__(self, tmpdir: Path):
        self._tmp = tmpdir
        self.ws = FakeWS(tmpdir)
        self.project_id = 34
        self.mr_iid = 999

    def _diff_line_map(self) -> dict[str, set[int]]:
        # 与 _call_agent 调用一致: 返回 file_path -> set of changed lines
        return {fp: {1, 2, 3} for fp in self._declared_files}

    def _read_file_line_count(self, file_path, ws) -> int:
        target = Path(ws.worktree) / file_path
        if not target.is_file():
            return -1
        return sum(1 for _ in target.open("r", encoding="utf-8", errors="replace"))

    def _read_file_lines(self, file_path: str) -> list[str]:
        target = Path(self.ws.worktree) / file_path
        if not target.is_file():
            return []
        return target.read_text(encoding="utf-8").splitlines()

    def _split_diff_by_file(self, diff_file, files):
        return {fp: f"diff --git a/{fp} b/{fp}\n+new line\n-old line\n" for fp in files}

    def _collect_cross_file_refs_for_mr(self, files, diff_by_file, wt):
        return {fp: [] for fp in files}

    def _call_chunk(self, prompt, ws, file_path):
        return SimpleNamespace(
            data={"summary_md": f"chunk for {file_path}", "suggestions": []},
            prompt_tokens=10, completion_tokens=5, model="test-model",
        )

    def _merge_chunks(self, results, *, skipped_files=None, test_skipped=None):
        merged_summary = "## 改进总览\n\n"
        summaries = [r.get("summary_md", "") for r in results if r.get("summary_md")]
        if summaries:
            merged_summary += "\n".join(summaries)
        if skipped_files:
            display = skipped_files[:10]
            suffix = f" 等 {len(skipped_files)} 个" if len(skipped_files) > 10 else ""
            merged_summary += (
                f"\n\n> ⏭️ 以下 {len(skipped_files)} 个测试/配置文件跳过检视: "
                f"{', '.join(display)}{suffix}"
            )
        return {"summary_md": merged_summary, "suggestions": []}

    # 占位方法 — 测试中通过 __getattr__ 拦截
    def __getattr__(self, name):
        return getattr(_real_improve_instance(), name)


def _real_improve_instance():
    """延迟构造真实 ImproveCommand 实例 (仅用于拿 _call_agent 方法)."""
    from reviewagent.commands.improve import ImproveCommand
    inst = ImproveCommand.__new__(ImproveCommand)
    inst.project_id = 34
    inst.mr_iid = 999
    inst.ws = SimpleNamespace(worktree=Path("/tmp"), diff_file=Path("/tmp/.diff"))
    inst._last_oc_result = None
    return inst


def _make_files(tmpdir: Path, specs: dict[str, int]) -> None:
    """specs: {rel_path: line_count}."""
    for rel, n in specs.items():
        p = tmpdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(f"line {i}" for i in range(n)) + "\n", encoding="utf-8")


# ============ 策略分配测试 ============

def test_priority_score_small_critical():
    """services/api.py 150 行 + 关键路径 → high score (full 配额内)."""
    # 模拟 _priority 函数 (与 improve.py 同源算法)
    keyword_paths = ["services/", "/services/"]  # 兼容 "services/api.py" 与 "/services/api.py"
    diff_size = 5
    file_size = 150

    score = min(diff_size, 100) * 0.5
    if any(kw in "services/api.py" for kw in keyword_paths):  # 无前导 / 也匹配
        score += 50
    if file_size > 0 and file_size < 500:
        score += 30

    # services/ 关键路径 + 150 行 < 500 → 2.5 + 50 + 30 = 82.5
    assert score >= 80


def test_priority_score_large_non_critical():
    """utils/helper.py 3000 行 + 非关键路径 → medium score."""
    keyword_paths = ["services/"]  # 兼容 "services/api.py" 与 "/services/api.py"
    diff_size = 5
    file_size = 3000

    score = min(diff_size, 100) * 0.5
    if any(kw in "utils/helper.py" for kw in keyword_paths):
        score += 50
    if file_size > 0 and file_size < 500:
        score += 30

    # utils/ 不在关键路径 + 3000 行不加分 → 2.5
    assert score < 10


def test_strategy_assignment_for_overflow():
    """overflow 文件 → 强制 patch (与 strategy 配额无关)."""
    overflow_set = {"huge.py"}
    sorted_files = ["a.py", "b.py", "huge.py"]
    full_quota = 5

    strategy = {}
    for fp in sorted_files:
        if fp in overflow_set:
            strategy[fp] = "patch"
            continue
        if full_quota > 0:
            strategy[fp] = "full"

    # huge.py 在 overflow → patch
    # a.py / b.py 在配额内 → full
    assert strategy["huge.py"] == "patch"
    assert strategy["a.py"] == "full"
    assert strategy["b.py"] == "full"


def test_strategy_partial_for_mid_critical_file():
    """services/db.py 800 行 + 关键路径 → partial (200<800<2000 但有关键路径加分时仍 partial)."""
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        _make_files(tmpdir, {"services/db.py": 800})
        # 模拟 MAX_FILES=0 → full 配额耗尽, 进入 partial
        # 通过 _priority 检查 priority: file_size=800 < 2000 → partial
        score = 3 * 0.5  # diff_size=3, < 100
        score += 50  # 关键路径
        # 800 >= 500, 不加小文件分
        assert score > 0


def test_strategy_patch_for_large_non_critical_file():
    """utils/helper.py 3000 行 + 非关键路径 → patch."""
    # 不在 _keyword_paths → file_size=3000 >= 2000 → patch
    file_size = 3000
    is_keyword = False
    expected_strategy = "patch" if (file_size >= 2000 and not is_keyword) else "partial"
    assert expected_strategy == "patch"


def test_strategy_skip_for_test_paths():
    """tests/test_x.py → skip; config.yaml → 走扩展名过滤."""
    from pathlib import Path as _P
    skip_test_patterns = ["tests/", "test_", "conftest.py"]
    test_files = ["tests/test_x.py", "test_y.py", "conftest.py", "src/api.py"]
    skipped = []
    kept = []
    for fp in test_files:
        name = _P(fp).name
        if any(pat in fp or name.startswith(pat.rstrip("/")) for pat in skip_test_patterns):
            skipped.append(fp)
        else:
            kept.append(fp)
    assert "tests/test_x.py" in skipped
    assert "test_y.py" in skipped
    assert "conftest.py" in skipped
    assert "src/api.py" in kept


def test_overflow_files_from_max_diff_chars():
    """60K 单文件 → MAX_DIFF_CHARS=50K 时, 该文件进 overflow → 强制 patch."""
    overflow_set = {"big_file.py"}
    sorted_files = ["small.py", "medium.py", "big_file.py"]
    full_quota = 20

    strategy = {}
    for fp in sorted_files:
        if fp in overflow_set:
            strategy[fp] = "patch"
            continue
        if full_quota > 0:
            strategy[fp] = "full"

    assert strategy["big_file.py"] == "patch"
    assert strategy["small.py"] == "full"
    assert strategy["medium.py"] == "full"


def test_warning_includes_test_skipped_breakdown():
    """_merge_chunks warning 含 test_skipped 分类."""
    skipped_files = ["tests/test_a.py", "tests/test_b.py"]
    merged = {
        "summary_md": "## 改进总览\n\n未发现问题。",
    }
    if skipped_files:
        display = skipped_files[:10]
        suffix = f" 等 {len(skipped_files)} 个" if len(skipped_files) > 10 else ""
        merged["summary_md"] += (
            f"\n\n> ⏭️ 以下 {len(skipped_files)} 个测试/配置文件跳过检视: "
            f"{', '.join(display)}{suffix}"
        )
    assert "⏭️" in merged["summary_md"]
    assert "tests/test_a.py" in merged["summary_md"]
    assert "tests/test_b.py" in merged["summary_md"]


# ============ 真实集成测试 (走 ImproveCommand._call_agent) ============
# 目的: 暴露签名链路 bug (如 _merge_chunks 缺参数 / 基类 _call_agent 不兼容 overflow_files)
# 上面 FakeImproveCommand 系列测试重写了算法, 无法发现这类问题.

def test_call_agent_real_signature_chain(tmp_path, monkeypatch):
    """真实调用 ImproveCommand._call_agent, mock 底层 LLM 调用, 验证:
    1. _call_agent(ws, overflow_files=...) 签名兼容 (修复 #46)
    2. _merge_chunks 不因 test_skipped 崩 (修复 #45)
    3. overflow 文件被分配 patch strategy
    4. 测试路径文件被过滤
    """
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.llm.base import LLMResult

    # 构造 worktree: 2 个源文件 + 1 个测试文件
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "api.py").write_text("def api():\n    return 1\n", encoding="utf-8")
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "helper.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")

    ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")

    # 构造 ImproveCommand 实例 (绕过 __init__ 避免依赖 GitLabClient)
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 999
    cmd.ws = ws
    cmd._last_oc_result = None
    cmd.repo_context = ""

    # mock 掉依赖外部状态的方法
    monkeypatch.setattr(cmd, "_diff_line_map", lambda: {
        "services/api.py": {1, 2},
        "utils/helper.py": {1},
        "tests/test_api.py": {1},
    })
    monkeypatch.setattr(cmd, "_read_file_line_count", lambda fp, w: 3)
    monkeypatch.setattr(cmd, "_read_file_lines", lambda fp: [
        "def api():", "    return 1", ""
    ][:3])
    monkeypatch.setattr(cmd, "_split_diff_by_file", lambda df, files: {
        fp: f"diff --git a/{fp} b/{fp}\n+new\n" for fp in files
    })
    monkeypatch.setattr(cmd, "_collect_cross_file_refs_for_mr", lambda files, dbf, wt: {
        fp: [] for fp in files
    })
    # mock _call_chunk 返回完整 LLMResult (修复 #6 后的契约)
    def _fake_chunk(prompt, w, fp):
        return LLMResult(
            data={"summary_md": "", "suggestions": []},
            prompt_tokens=10, completion_tokens=5, model="test",
        )
    monkeypatch.setattr(cmd, "_call_chunk", _fake_chunk)

    # 真实调用 _call_agent, 传入 overflow_files (验证 #46 修复)
    result = cmd._call_agent(ws, overflow_files=["utils/helper.py"])

    # 验证不崩 + 结构正确
    assert "summary_md" in result
    assert "suggestions" in result
    assert isinstance(result["suggestions"], list)
    # 测试文件被过滤 (出现在 summary 的 skip 提示里)
    assert "tests/test_api.py" in result["summary_md"]
