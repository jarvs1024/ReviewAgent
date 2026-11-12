"""Task 6 — 仓库规则模块迁移、请求级 repo overlay 与 `rules check` CLI."""
from __future__ import annotations

import hashlib

from reviewagent.cli import main
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.repo import (
    INSTRUCTION_FILES_INTRO,
    TRUNCATION_MARKER,
    extract_rule_keys,
    parse_repo_overlay,
    render_instruction_files,
)
from reviewagent.scenario.rules.resolver import assemble_base

VERBATIM_SHA256 = "963ce3843726312aae2647371f98518e716add4bf96f349b58ad62b2cb97ba96"


# ---------------- 迁移：渲染 / 键提取（行为不变） ----------------


def test_instruction_files_render_escapes_and_truncates():
    # 行数预算只够放 3 行正文 + truncation marker（迁移前行为）
    text = render_instruction_files({"AGENTS.md": "# hi\n" + "x\n" * 10}, max_lines=12)
    assert "<instruction_files>" in text
    assert TRUNCATION_MARKER in text
    assert "truncated" in text
    assert text.count("x") == 2  # available-1 行正文后立刻截断


def test_instruction_files_render_full_output_shape():
    """未超预算时逐行原样保留（迁移不得改变输出字节）."""
    text = render_instruction_files({"docs/AGENTS.md": "# hi\nrule\n"})
    assert text.splitlines() == [
        INSTRUCTION_FILES_INTRO,
        "<instruction_files>",
        '<file path="docs/AGENTS.md" scope="docs">',
        "`````markdown",
        "# hi",
        "rule",
        "`````",
        "</file>",
        "",
        "</instruction_files>",
    ]


def test_instruction_files_render_empty_and_tight_budget():
    assert render_instruction_files({}) == ""
    # 预算装不下 file 头尾 → 只输出空块（与原实现逐字一致，不抛错）
    tight = render_instruction_files({"a.md": "x\n"}, max_lines=3)
    assert tight == f"{INSTRUCTION_FILES_INTRO}\n<instruction_files>\n</instruction_files>"
    # 已处于循环内但预算不足 → 保留已输出的部分 + 关闭标签
    assert render_instruction_files({"a.md": "x\n"}, max_lines=8) == tight


def test_instruction_files_render_escapes_path_and_scope():
    text = render_instruction_files({'weird&<"dir/AGENTS.md': "# hi\n"})
    assert 'path="weird&amp;&lt;&quot;dir/AGENTS.md"' in text
    assert 'scope="weird&amp;&lt;&quot;dir"' in text


def test_instruction_files_render_escalates_fence_when_content_has_default():
    content = "before\n`````\nafter\n"
    text = render_instruction_files({"AGENTS.md": content})
    lines = text.splitlines()
    assert "``````markdown" in lines
    assert lines.count("``````") == 1
    # 内容里的默认围栏原样保留
    assert "`````" in lines


def test_instruction_files_render_empty_below_min_budget():
    assert render_instruction_files({"a.md": "x\n"}, max_lines=2) == ""
    assert render_instruction_files({"a.md": "x\n"}, max_lines=0) == ""


def test_extract_rule_keys_in_order_without_duplicates():
    text = (
        "先看 SSD-RULE-NO-LOG-EXC，再看 SSD-RULE-OTHER。\n"
        "重复 SSD-RULE-NO-LOG-EXC 与 SSD-RULE-FORBIDDEN-WILDCARD-IMPORT。\n"
        "不要误抽: SSD-RULE-lower。\n"
    )
    assert extract_rule_keys(text) == [
        "SSD-RULE-NO-LOG-EXC",
        "SSD-RULE-OTHER",
        "SSD-RULE-FORBIDDEN-WILDCARD-IMPORT",
    ]
    assert extract_rule_keys("") == []


def test_extract_rule_keys_auto_prefix_accepts_any_uppercase_namespace():
    """默认 auto 前缀不绑定具体仓库：ACME-RULE-* 与 SSD-RULE-* 同形同权."""
    text = "ACME-RULE-CASE-ALLURE 与 SSD-RULE-NO-LOG-EXC，重复 ACME-RULE-CASE-ALLURE"
    assert extract_rule_keys(text) == ["ACME-RULE-CASE-ALLURE", "SSD-RULE-NO-LOG-EXC"]


# ---------------- overlay：场景规则同键覆盖 / 仓库新增 / 忽略自由文本 ----------------


def test_repo_overlay_structured_entry_overrides_scenario_rule():
    profile = load_profile(None)
    base = assemble_base(profile)
    files = {"AGENTS.md": "| 键 | 严重度 | 摘要 |\n|---|---|---|\n| `R-LOG` | high | 仓库要求 |\n"}
    overlay = parse_repo_overlay(files, profile=profile, base=base)
    merged = base.with_repo(overlay)
    assert merged.index_by_key["R-LOG"].severity == "high"
    assert merged.index_by_key["R-LOG"].priority == 1000
    assert merged.index_by_key["R-LOG"].title == "仓库要求"
    assert merged.index_by_key["R-LOG"].source == {"kind": "repo", "path": "AGENTS.md"}
    # 仓库规则原文走 prompt 的 {{repo_rules}}，不得改写场景规则正文
    assert merged.verbatim_body == base.verbatim_body
    assert merged.fingerprint != base.fingerprint
    # overlay 只换索引，不得泄漏到 rules 顺序之外（R-LOG 在原位）
    assert [rule.key for rule in merged.rules] == [rule.key for rule in base.rules]


def test_repo_overlay_override_preserves_base_rule_fields():
    profile = load_profile(None)
    base = assemble_base(profile)
    original = base.index_by_key["R-LOG"]
    overlay = parse_repo_overlay(
        {"AGENTS.md": "| `R-LOG` | high | 仓库要求 |\n"}, profile=profile, base=base
    )
    assert len(overlay) == 1
    rule = overlay[0]
    # 只换元信息，基础规则的其余字段必须保留
    assert (rule.weight, rule.applies_to, rule.body) == (
        original.weight,
        original.applies_to,
        original.body,
    )
    assert (rule.examples, rule.actions, rule.aliases, rule.detect_hints) == (
        original.examples,
        original.actions,
        original.aliases,
        original.detect_hints,
    )
    assert (rule.severity, rule.title, rule.priority) == ("high", "仓库要求", 1000)
    assert rule.source == {"kind": "repo", "path": "AGENTS.md"}


def test_repo_overlay_adds_repo_prefixed_rule():
    profile = load_profile(None)
    base = assemble_base(profile)
    files = {
        ".agents/rules/extra.md": (
            "| 键 | 严重度 | 摘要 |\n"
            "|---|---|---|\n"
            "| `SSD-RULE-NO-DEBUG-PRINT` | critical | 禁止调试打印 |\n"
        )
    }
    overlay = parse_repo_overlay(files, profile=profile, base=base)
    assert [rule.key for rule in overlay] == ["SSD-RULE-NO-DEBUG-PRINT"]
    merged = base.with_repo(overlay)
    added = merged.index_by_key["SSD-RULE-NO-DEBUG-PRINT"]
    assert (added.severity, added.priority) == ("critical", 1000)
    assert added.source == {"kind": "repo", "path": ".agents/rules/extra.md"}
    assert merged.rules[-1].key == "SSD-RULE-NO-DEBUG-PRINT"  # 新增键追加在末尾
    assert merged.verbatim_body == base.verbatim_body
    assert merged.fingerprint.startswith(base.fingerprint + ":repo:")


def test_repo_overlay_severity_falls_back_to_medium():
    profile = load_profile(None)
    base = assemble_base(profile)
    files = {"AGENTS.md": "| `SSD-RULE-LOOSE` | 严重 | 说明 |\n"}
    overlay = parse_repo_overlay(files, profile=profile, base=base)
    assert [(rule.key, rule.severity, rule.title) for rule in overlay] == [
        ("SSD-RULE-LOOSE", "medium", "说明")
    ]


def test_repo_overlay_dedupes_repeated_keys_last_wins():
    profile = load_profile(None)
    base = assemble_base(profile)
    files = {
        "AGENTS.md": "| `SSD-RULE-DUP` | low | 先 |\n",
        ".agents/rules/more.md": "| `SSD-RULE-DUP` | high | 后 |\n",
    }
    overlay = parse_repo_overlay(files, profile=profile, base=base)
    assert [(rule.key, rule.severity, rule.title) for rule in overlay] == [
        ("SSD-RULE-DUP", "high", "后")
    ]


def test_repo_free_text_is_not_a_rule():
    profile = load_profile(None)
    overlay = parse_repo_overlay(
        {"AGENTS.md": "请使用 with 管理资源。\n"}, profile=profile, base=assemble_base(profile)
    )
    assert overlay == ()


def test_repo_overlay_ignores_fenced_table_rows():
    profile = load_profile(None)
    base = assemble_base(profile)
    content = (
        "示例：\n"
        "````markdown\n"
        "| R-LOG | low | example |\n"
        "````\n"
        "\n"
        "| `SSD-RULE-UNFENCED` | high | 围栏外生效 |\n"
    )
    overlay = parse_repo_overlay({"AGENTS.md": content}, profile=profile, base=base)
    assert [rule.key for rule in overlay] == ["SSD-RULE-UNFENCED"]


def test_repo_overlay_ignores_unknown_builtin_and_unrelated_tables():
    profile = load_profile(None)
    base = assemble_base(profile)
    files = {
        "AGENTS.md": (
            "| 键 | 严重度 | 摘要 |\n"
            "|---|---|---|\n"
            "| `R-NOT-IN-SCENARIO` | high | 未装配的内置键 |\n"
            "| 文件名 | 用途 |\n"
            "| `docs/x.md` | 文档 |\n"
        )
    }
    assert parse_repo_overlay(files, profile=profile, base=base) == ()


def test_with_repo_empty_returns_self():
    base = assemble_base(load_profile(None))
    assert base.with_repo(()) is base
    assert base.fingerprint == assemble_base(load_profile(None)).fingerprint


def test_with_repo_fingerprint_distinguishes_same_key_overlay_fields():
    profile = load_profile(None)
    base = assemble_base(profile)
    low = parse_repo_overlay(
        {"AGENTS.md": "| `R-LOG` | low | 低 |\n"}, profile=profile, base=base
    )
    high = parse_repo_overlay(
        {"AGENTS.md": "| `R-LOG` | high | 高 |\n"}, profile=profile, base=base
    )
    low_fp = base.with_repo(low).fingerprint
    high_fp = base.with_repo(high).fingerprint
    assert low_fp != high_fp
    assert low_fp.startswith(base.fingerprint + ":repo:")
    assert high_fp.startswith(base.fingerprint + ":repo:")


def test_repo_overlay_does_not_change_scenario_verbatim_checksum():
    base = assemble_base(load_profile(None))
    assert hashlib.sha256(base.verbatim_body.encode()).hexdigest() == VERBATIM_SHA256
    merged = base.with_repo(
        parse_repo_overlay(
            {"AGENTS.md": "| `R-LOG` | high | 仓库要求 |\n"}, profile=load_profile(None), base=base
        )
    )
    assert hashlib.sha256(merged.verbatim_body.encode()).hexdigest() == VERBATIM_SHA256


# ---------------- 运行期接入：repo overlay → 请求级 RuleSet ----------------


def _improve_cmd(monkeypatch, files):
    from reviewagent.commands.improve import ImproveCommand

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.gitlab = object()
    monkeypatch.setattr(
        "reviewagent.scenario.rules.repo.fetch_rule_files",
        lambda *_a, **_k: files,
    )
    return cmd


def test_improve_runtime_ruleset_merges_repo_rules(monkeypatch):
    """仓库规则表在运行期进入 RuleSet：同键覆盖 + 新增键 + 请求级指纹."""
    profile = load_profile(None)
    cmd = _improve_cmd(
        monkeypatch,
        {
            "AGENTS.md": (
                "| 键 | 严重度 | 摘要 |\n"
                "|---|---|---|\n"
                "| `R-LOG` | high | 仓库要求 |\n"
                "| `SSD-RULE-NO-DEBUG-PRINT` | critical | 禁止调试打印 |\n"
            )
        },
    )
    _profile, rule_set, _policy = cmd._lazy_deps()
    base = assemble_base(profile)
    assert rule_set.index_by_key["R-LOG"].severity == "high"
    assert rule_set.index_by_key["R-LOG"].title == "仓库要求"
    assert rule_set.index_by_key["SSD-RULE-NO-DEBUG-PRINT"].priority == 1000
    assert rule_set.fingerprint.startswith(base.fingerprint + ":repo:")
    # 规则正文仍只有场景包一份（仓库原文经 {{repo_rules}} 注入）
    assert rule_set.verbatim_body == base.verbatim_body


def test_improve_runtime_ruleset_without_repo_rules_is_base(monkeypatch):
    """仓库没有结构化规则表 → 沿用基础 RuleSet（指纹不变，不击穿增量复用）."""
    profile = load_profile(None)
    cmd = _improve_cmd(monkeypatch, {})
    _profile, rule_set, _policy = cmd._lazy_deps()
    assert rule_set.fingerprint == assemble_base(profile).fingerprint


def test_improve_runtime_ruleset_degrades_when_repo_fetch_fails(monkeypatch):
    """仓库规则读取失败不得阻塞检视：降级为基础 RuleSet."""
    profile = load_profile(None)
    cmd = _improve_cmd(monkeypatch, {})

    def _boom(*_args, **_kwargs):
        raise RuntimeError("gitlab down")

    monkeypatch.setattr("reviewagent.scenario.rules.repo.fetch_rule_files", _boom)
    _profile, rule_set, _policy = cmd._lazy_deps()
    assert rule_set.fingerprint == assemble_base(profile).fingerprint


# ---------------- rules check CLI ----------------


def test_rules_check_default_profile(capsys, monkeypatch):
    monkeypatch.delenv("REVIEW_PROFILE", raising=False)
    assert main(["rules", "check"]) == 0
    out = capsys.readouterr().out
    assert "R-LOG" in out
    assert "ruleset_fingerprint=" in out
    assert "render_checksum=" in out
    # 每条规则一行 key\tseverity\ttitle
    rule_lines = [line for line in out.splitlines() if line.startswith("R-LOG\t")]
    assert rule_lines == ["R-LOG\tmedium\t日志可观测"]
    assert f"render_checksum={VERBATIM_SHA256}" in out


def test_rules_check_writes_no_instance_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    assert main(["rules", "check", "--profile", "ssd-python-autotest"]) == 0
    assert not (tmp_path / "instance.json").exists()


def test_rules_check_unknown_profile_fails_cleanly(capsys):
    assert main(["rules", "check", "--profile", "nope"]) == 2
    err = capsys.readouterr().err
    assert "rules check failed" in err
    assert "nope" in err
    assert "Traceback" not in err
