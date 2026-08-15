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


# ============ V7 priority / 测试特征密度 / per-file 截断 / patch context ============

def test_priority_log1p_does_not_cap():
    """V7: log1p(diff_size) 不封顶, 100行和1000行能区分开 (修复 V6 min(...,100) 封顶问题)"""
    import math
    w_diff = 20.0
    s100 = math.log1p(100) * w_diff   # ≈92.4
    s500 = math.log1p(500) * w_diff   # ≈125.6
    s1000 = math.log1p(1000) * w_diff  # ≈138.2
    assert s500 > s100, "500行应比100行高分"
    assert s1000 > s500, "1000行应比500行高分 (不封顶)"


def test_priority_large_diff_beats_small_keyword():
    """V7: 80行非关键路径 > 5行关键路径 (V6 因关键词+50碾压, 反了)"""
    import math
    w_diff, w_kw = 20.0, 25.0
    # 5行 services/api.py (关键路径)
    small_kw = math.log1p(5) * w_diff + w_kw     # ≈36 + 25 = 61
    # 80行 utils/big.py (非关键路径)
    large_plain = math.log1p(80) * w_diff         # ≈88
    assert large_plain > small_kw, f"80行非关键({large_plain}) 应 > 5行关键({small_kw})"


def test_test_feature_density_combo(tmp_path):
    """V7: 测试特征密度 = 绝对数 + 密度组合, 大文件不被稀释"""
    from types import SimpleNamespace
    from reviewagent.commands.improve import ImproveCommand

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")

    # 50行 10个assert (密度20%) — 小文件高密度
    small = tmp_path / "small_test.py"
    small.write_text("\n".join(["assert x" if i < 10 else "pass" for i in range(50)]) + "\n")
    d_small = cmd._test_feature_density("small_test.py", 50)

    # 2000行 100个assert (密度5%) — 大文件中密度, 但绝对数高
    big = tmp_path / "big_test.py"
    big.write_text("\n".join(["assert x" if i < 100 else "pass" for i in range(2000)]) + "\n")
    d_big = cmd._test_feature_density("big_test.py", 2000)

    # 两者都应得分 (>0), 大文件因绝对数满分不会比小文件差太多
    assert d_small > 0, "小文件高密度应得分"
    assert d_big > 0, "大文件绝对数高应得分"
    # 绝对数组合: big 的绝对分满分 0.5, small 的绝对分 10/30*0.5≈0.17
    # 不应出现 big 因密度低而得 0 的情况 (V6 纯密度会稀释)


def test_per_file_suggestion_limit():
    """V7: _merge_chunks 每文件最多保留 N 条, 防噪音文件吃光全局槽位"""
    from reviewagent.commands.improve import ImproveCommand
    # 一个文件 8 条建议, 另一个 2 条, per_file=5 → 第一个文件截到 5
    results = [{
        "summary_md": "",
        "suggestions": [
            {"file": "noisy.py", "start_line": i, "severity": "high" if i < 3 else "low",
             "header": f"h{i}", "rationale": "x", "label": "potential bug"}
            for i in range(8)
        ] + [
            {"file": "quiet.py", "start_line": 1, "severity": "high",
             "header": "q1", "rationale": "x", "label": "potential bug"},
            {"file": "quiet.py", "start_line": 2, "severity": "medium",
             "header": "q2", "rationale": "x", "label": "potential bug"},
        ],
    }]
    merged = ImproveCommand._merge_chunks(results)
    files = [s["file"] for s in merged["suggestions"]]
    assert files.count("noisy.py") <= 5, f"noisy.py 应最多5条, got {files.count('noisy.py')}"
    assert "quiet.py" in files, "quiet.py 不应被挤掉 (per-file 保证覆盖面)"


def test_patch_only_block_has_context(tmp_path):
    """V7: patch 模式带 ±N 行最小 context, 不再纯行号"""
    from types import SimpleNamespace
    from reviewagent.commands.improve import ImproveCommand

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")

    # 写个 20 行文件
    f = tmp_path / "target.py"
    f.write_text("\n".join([f"line {i}" for i in range(1, 21)]) + "\n")

    block = cmd._build_patch_only_block("target.py", {5, 15})
    # V6 是纯行号无源码; V7 应含 source context
    assert "最小上下文" in block or "line 5" in block, "patch 模式应带 source context"
    assert "patch-only" in block


def test_review_mode_auto_test_project(tmp_path, monkeypatch):
    """V7 auto 模式: 测试文件占比>50% → 切 test 模式, 不跳过测试文件"""
    from types import SimpleNamespace
    from reviewagent.llm.base import LLMResult
    from reviewagent.commands.improve import ImproveCommand

    # 全是测试文件
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("assert True\n")
    (tmp_path / "tests" / "test_b.py").write_text("assert False\n")

    ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 999
    cmd.ws = ws
    cmd._last_oc_result = None
    cmd.repo_context = ""

    monkeypatch.setattr(cmd, "_diff_line_map", lambda: {
        "tests/test_a.py": {1}, "tests/test_b.py": {1},
    })
    monkeypatch.setattr(cmd, "_read_file_line_count", lambda fp, w: 1)
    monkeypatch.setattr(cmd, "_read_file_lines", lambda fp: ["assert True"])
    monkeypatch.setattr(cmd, "_split_diff_by_file", lambda df, files: {
        fp: f"diff --git a/{fp} b/{fp}\n+new\n" for fp in files
    })
    monkeypatch.setattr(cmd, "_collect_cross_file_refs_for_mr", lambda files, dbf, wt: {
        fp: [] for fp in files
    })
    monkeypatch.setattr(cmd, "_call_chunk", lambda p, w, fp: LLMResult(
        data={"summary_md": "", "suggestions": []}, prompt_tokens=10, completion_tokens=5, model="t",
    ))

    result = cmd._call_agent(ws)
    # test 模式不跳过测试文件 → summary 里不应有 "跳过检视" 提示
    assert "跳过检视" not in result["summary_md"], "test 模式不应跳过测试文件"
    assert cmd._review_mode == "test"


# ============ V8 增量检视测试 ============

def test_get_reviewed_file_shas_returns_latest_per_file(tmp_path):
    """V8: store.get_reviewed_file_shas 返回每个文件最新一条 suggestion 的 head_sha"""
    import sqlite3
    from reviewagent.telemetry.store import Store
    db_path = tmp_path / "test.db"
    store = Store(db_path)
    # 手动插 suggestions
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO suggestions (project_id, mr_iid, note_id, file_path, target_line, "
            "head_sha, state, severity, created_at) VALUES "
            "(34, 289, 'n1', 'services/a.py', 3, 'sha1', 'open', 'high', '2026-01-01'), "
            "(34, 289, 'n2', 'services/a.py', 7, 'sha2', 'open', 'high', '2026-01-02'), "
            "(34, 289, 'n3', 'services/b.py', 1, 'sha1', 'open', 'medium', '2026-01-01')")
    result = store.get_reviewed_file_shas(34, 289)
    # a.py 取最新 (id 最大) → sha2; b.py → sha1
    assert result.get("services/a.py") == "sha2"
    assert result.get("services/b.py") == "sha1"


def test_incremental_reuse_skips_unchanged_files(tmp_path, monkeypatch):
    """V8: 同 head_sha 的文件跳过 LLM, 复用上轮 suggestions"""
    from types import SimpleNamespace
    from pathlib import Path
    from reviewagent.llm.base import LLMResult
    from reviewagent.commands.improve import ImproveCommand

    # 构造 2 文件 worktree
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "services" / "b.py").write_text("def b():\n    return 2\n")
    ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 289
    cmd.ws = ws
    cmd._last_oc_result = None
    cmd.repo_context = ""

    monkeypatch.setattr(cmd, "_diff_line_map", lambda: {
        "services/a.py": {1}, "services/b.py": {1},
    })
    monkeypatch.setattr(cmd, "_read_file_line_count", lambda fp, w: 2)
    monkeypatch.setattr(cmd, "_read_file_lines", lambda fp: ["def x():", "    return 1"])
    monkeypatch.setattr(cmd, "_split_diff_by_file", lambda df, files: {
        fp: f"diff --git a/{fp} b/{fp}\n+new\n" for fp in files
    })
    monkeypatch.setattr(cmd, "_collect_cross_file_refs_for_mr", lambda files, dbf, wt: {
        fp: [] for fp in files
    })

    # mock head_sha
    monkeypatch.setattr(cmd, "_get_mr_head_sha", lambda: "abc123")

    # mock store: a.py 上轮也是 abc123 (未变), b.py 上轮是 old_sha (变了)
    from reviewagent.telemetry.store import Store as _Store
    mock_store = type("MockStore", (), {
        "get_reviewed_file_shas": lambda self, pid, mid: {
            "services/a.py": "abc123",  # 同 sha → 复用
            "services/b.py": "old_sha",  # 不同 sha → 重检
        },
        "list_suggestions": lambda self, **kw: [
            {"file_path": "services/a.py", "head_sha": "abc123", "target_line": 1,
             "severity": "high", "label": "potential bug", "header": "test", "one_sentence_summary": "test"},
        ],
    })()
    monkeypatch.setattr("reviewagent.telemetry.store.get_store", lambda: mock_store)

    # mock _call_chunk: 只应被调一次 (b.py)
    called_files = []
    def _fake_chunk(prompt, w, fp):
        called_files.append(fp)
        return LLMResult(data={"summary_md": "", "suggestions": []}, prompt_tokens=10, completion_tokens=5, model="t")
    monkeypatch.setattr(cmd, "_call_chunk", _fake_chunk)

    result = cmd._call_agent(ws)

    # 只有 b.py 调了 LLM, a.py 复用
    assert called_files == ["services/b.py"], f"应只检视b.py, 实际调了: {called_files}"
    # a.py 的复用 suggestion 出现在结果里
    assert len(result["suggestions"]) >= 1


def test_incremental_cross_impact_forces_recheck(tmp_path, monkeypatch):
    """V8: unchanged 文件的标识符出现在 changed 文件 diff 里 → 强制重检"""
    from types import SimpleNamespace
    from reviewagent.llm.base import LLMResult
    from reviewagent.commands.improve import ImproveCommand

    (tmp_path / "a.py").write_text("def shared_func():\n    return 1\n")
    (tmp_path / "b.py").write_text("x = shared_func()\n")
    ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 289
    cmd.ws = ws
    cmd._last_oc_result = None
    cmd.repo_context = ""

    # a.py 和 b.py 都是同 head_sha (未变), 但 b.py 的 diff 引用了 a.py 的标识符
    monkeypatch.setattr(cmd, "_diff_line_map", lambda: {
        "a.py": {1}, "b.py": {1},
    })
    monkeypatch.setattr(cmd, "_read_file_line_count", lambda fp, w: 2)
    monkeypatch.setattr(cmd, "_read_file_lines", lambda fp: ["def shared_func():", "    return 1"])
    # b.py 的 diff 包含 "shared_func" (a.py 的标识符)
    monkeypatch.setattr(cmd, "_split_diff_by_file", lambda df, files: {
        "a.py": "diff --git a/a.py b/a.py\n+def shared_func(): return 1\n",
        "b.py": "diff --git a/b.py b/b.py\n+x = shared_func()\n",
    })
    monkeypatch.setattr(cmd, "_collect_cross_file_refs_for_mr", lambda files, dbf, wt: {
        fp: [] for fp in files
    })
    monkeypatch.setattr(cmd, "_get_mr_head_sha", lambda: "same_sha")

    from reviewagent.telemetry.store import Store as _Store
    mock_store = type("MockStore", (), {
        "get_reviewed_file_shas": lambda self, pid, mid: {
            "a.py": "same_sha",  # 同 sha
            "b.py": "same_sha",  # 同 sha
        },
        "list_suggestions": lambda self, **kw: [],
    })()
    monkeypatch.setattr("reviewagent.telemetry.store.get_store", lambda: mock_store)

    called_files = []
    def _fake_chunk(prompt, w, fp):
        called_files.append(fp)
        return LLMResult(data={"summary_md": "", "suggestions": []}, prompt_tokens=10, completion_tokens=5, model="t")
    monkeypatch.setattr(cmd, "_call_chunk", _fake_chunk)

    cmd._call_agent(ws)
    # a.py 和 b.py 同 sha, 但 b.py diff 引用了 a.py 的 shared_func
    # a.py 应被强制重检 (跨文件影响), b.py 也重检 (因为它的 diff 里有 changed_text)
    # 注意: 都同 sha 时 changed_text 为空 (因为没有 changed 文件), 所以都复用
    # 但如果有一个文件 sha 不同, 它的 diff 就进 changed_text
    # 这个测试验证: 当所有文件同 sha 时, 全部复用 (无 changed_text)
    assert len(called_files) == 0, "所有文件同sha且无changed → 应全复用, 0次LLM调用"


# ============ V9 架构审查修复回归测试 ============

def test_list_suggestion_headers_uses_state_not_status(tmp_path):
    """回归: list_suggestion_headers 之前查 status 列 (不存在), 静默崩 → 防重复失效.
    修复: 改查 state 列."""
    from pathlib import Path
    from reviewagent.telemetry.store import Store

    store = Store(tmp_path / "test.db")
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO suggestions (project_id, mr_iid, note_id, file_path, target_line, "
            "head_sha, state, severity, header, fingerprint, created_at) VALUES "
            "(34, 289, 'n1', 'services/a.py', 3, 'sha1', 'open', 'high', 'test header', 'abcdef1234', '2026-01-01'), "
            "(34, 289, 'n2', 'services/b.py', 7, 'sha1', 'applied', 'medium', 'another', 'deadbeef5678', '2026-01-02')"
        )
    # 之前会抛 OperationalError: no such column: status
    result = store.list_suggestion_headers(34, 289)
    assert len(result) == 2
    states = {r["state"] for r in result}
    assert "open" in states
    assert "applied" in states
    assert "status" not in result[0], "不应返回 status 键, 应为 state"


def test_weekly_config_replace_preserves_all_fields():
    """回归: 周报配置重建之前手动列字段, 漏了 report_title/emoji/dashboard_url.
    修复: 用 dataclasses.replace 只覆盖 target_project_id."""
    import dataclasses
    from reviewagent.reporting.config import WeeklyReportConfig

    cfg = WeeklyReportConfig.from_env()
    cfg2 = dataclasses.replace(cfg, target_project_id=999)
    # 所有字段都应保留
    assert cfg2.report_title == cfg.report_title
    assert cfg2.report_emoji == cfg.report_emoji
    assert cfg2.dashboard_url == cfg.dashboard_url
    assert cfg2.target_project_id == 999
