"""反向规则契约：场景包必须声明「不报边界」，提示词不得保留无边界的强制措辞.

背景:
    场景包原先只有正向规则表（发现什么就报什么），没有「不报什么」的边界，
    导致纯注释 / 格式 / 等价改写等低价值建议稀释真正的问题。本测试锁定：
    边界包必须先于正向规则注入；例外条款（安全 / 仓库规则）必须在位；
    improve.md 不得再出现无边界的「每个可疑 bug 都必须报」。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = REPO_ROOT / "profiles" / "ssd-python-autotest"


def _profile():
    from reviewagent.scenario.profile import load_profile

    return load_profile("ssd-python-autotest")


def _rule_set():
    from reviewagent.scenario.rules.resolver import assemble_base

    return assemble_base(_profile())


def test_noise_boundary_pack_loaded_before_general_rules():
    names = [Path(source["path"]).name for source in _rule_set().sources]
    assert names[0] == "noise_boundary.md", names
    assert "general_rules.md" in names, names


def test_boundary_rendered_before_positive_rule_table():
    from reviewagent.scenario.rules.render import render_rule_block

    body = render_rule_block(_rule_set(), profile=_profile())
    assert "⛔ 检视边界" in body
    assert body.index("⛔ 检视边界") < body.index("| 规则键 | 严重度 | 类别")


def test_boundary_declares_noise_classes_and_exceptions():
    text = (PROFILE_ROOT / "rules" / "noise_boundary.md").read_text(encoding="utf-8")
    for needle in (
        "安全类",
        "纯文本",
        "描述类",
        "非代码强相关",
        "等价改写",
        "影响不大",
        "仓库自定义规则明确要求的条目",
        "low 级自检",
    ):
        assert needle in text, needle


def test_improve_prompt_has_no_unbounded_mandate():
    text = (PROFILE_ROOT / "prompts" / "improve.md").read_text(encoding="utf-8")
    assert "检视边界" in text
    assert "每个有影响的问题都必须有一条 suggestion" in text
    assert "每个可疑 bug 都必须有一条 suggestion" not in text
    assert "所有 severity 都产 suggestion" not in text


def test_general_rules_has_no_unbounded_mandate():
    """general_rules.md 同样不得保留无边界措辞.

    Why 需要这条: `general_rules.md` 里也有一句「所有 severity 都产 suggestion」——
    它与「⛔ 检视边界」的 low 两问自检**直接矛盾**（low 必须先过两问才能报）.
    只检查 improve.md 会漏掉这一处 —— 规则包正文同样是喂给 LLM 的指令源。
    """
    text = (PROFILE_ROOT / "rules" / "general_rules.md").read_text(encoding="utf-8")
    assert "所有 severity 都产 suggestion" not in text, (
        "general_rules.md 仍保留「所有 severity 都产 suggestion」—— "
        "与 noise_boundary 的 low 两问自检矛盾"
    )
    assert "命中即产 suggestion" not in text, (
        "general_rules.md 仍保留「命中即产 suggestion」—— "
        "与「命中且有影响才产」矛盾"
    )


def test_boundary_pack_is_actually_injected_into_improve_prompt():
    """降噪边界必须真的进了 improve 的提示词 —— 不能只存在于文件里.

    Why: `profile.yaml` 里配了 rules 列表，但若装配/渲染环节把它丢掉，
    LLM 根本看不到边界 → 降噪完全失效（而所有文件级契约测试照样绿）.
    """
    from reviewagent.commands.improve import ImproveCommand

    block = ImproveCommand._rule_block_text()
    assert "⛔ 检视边界" in block, "降噪边界未注入 improve 规则块"
    assert block.index("⛔ 检视边界") < block.index("| 规则键 | 严重度 | 类别"), (
        "降噪边界必须排在正向规则表之前"
    )


def test_boundary_gates_general_rules_only():
    """边界必须卡在「通用规则」之前, 但不得挡在「仓库自定义规则」之前.

    Why 需要这条: 降噪边界的适用范围是**通用 / 风格层**。若装配顺序被改坏
    （例如边界排到通用规则之后, 或排到仓库规则之前）, 会出现两种静默失效:
    边界形同虚设, 或仓库自定义规则被误裁。
    这里锁住两个分支的物理位置关系:
      * 无仓库规则 → 边界在通用规则之前;
      * 有仓库规则 → 仓库规则 → 边界 → 通用规则, 且仓库规则段的
        「命中即产 suggestion」必须保留（强制例外不得被裁）。
    """
    from reviewagent.review.assembly import build_rules_block
    from reviewagent.scenario.profile import load_profile

    profile = load_profile("ssd-python-autotest")
    general = _rule_set().verbatim_body

    boundary = "⛔ 检视边界"
    general_head = "🟡 优先 3 — 通用规则"
    repo_head = "## 🟠 优先 2 — 仓库自定义规则"

    no_repo = build_rules_block("", general, profile=profile)
    assert boundary in no_repo and general_head in no_repo
    assert no_repo.index(boundary) < no_repo.index(general_head), (
        "无仓库规则时, 降噪边界必须排在通用规则之前"
    )

    with_repo = build_rules_block("<rule_index>\nSSD-RULE-X\n</rule_index>", general, profile=profile)
    assert with_repo.index(repo_head) < with_repo.index(boundary) < with_repo.index(general_head), (
        "有仓库规则时, 顺序必须是 仓库规则 → 降噪边界 → 通用规则"
    )
    assert "命中即产 suggestion" in with_repo, (
        "仓库自定义规则段的「命中即产 suggestion」被裁掉了 —— 强制例外失效"
    )


def test_misleading_comment_is_first_class_exception():
    """会误导的过期注释必须列进「强制例外」清单, 不能只写在不报清单的表格例外列.

    Why 需要这条: 真实业务 A/B 实测（同一份 diff, 边界开启 vs 边界关闭）发现
    —— 边界开启时一条**会误导**的过期注释被整条裁掉, 而关闭边界的基线反而报了出来
    （边界比基线还漏报）。根因: 「仍要报」只写在不报清单的表格单元格里,
    不在模型优先读的「强制例外」清单中, 模型按「注释 → 纯文本 → 丢弃」处理。
    """
    boundary = (PROFILE_ROOT / "rules" / "noise_boundary.md").read_text(encoding="utf-8")
    assert "#### 强制例外" in boundary
    exception_block = boundary.split("#### 强制例外")[1].split("\n#### ")[0]
    assert "stale_comment" in exception_block, (
        "会误导的过期注释必须在「强制例外」清单里, 否则会被当纯文本丢弃"
    )
    assert "会误导" in exception_block


def test_low_must_pass_boundary_self_check():
    """low 级必须绑定两问自检（不然 low 会重新变成噪音源）."""
    boundary = (PROFILE_ROOT / "rules" / "noise_boundary.md").read_text(encoding="utf-8")
    assert "不改它，具体会出什么后果" in boundary
    assert "这个后果能在本次 diff 内验证吗" in boundary
    improve = (PROFILE_ROOT / "prompts" / "improve.md").read_text(encoding="utf-8")
    assert "两问" in improve, "improve.md 必须显式引用 low 两问"


def test_general_rules_low_scope_points_to_boundary():
    text = (PROFILE_ROOT / "rules" / "general_rules.md").read_text(encoding="utf-8")
    assert "⛔ 检视边界" in text
    assert "纯拼写 / 格式 / 命名口味不报" in text


# ---------------------------------------------------------------------------
# 安全类降噪（内网测试台内部代码 → 无外部攻击面）
# ---------------------------------------------------------------------------


def _boundary_text() -> str:
    return (PROFILE_ROOT / "rules" / "noise_boundary.md").read_text(encoding="utf-8")


def _exception_block() -> str:
    """「强制例外」段正文 —— 强制例外里的条目一律**不**受降噪影响."""
    text = _boundary_text()
    assert "#### 强制例外" in text
    return text.split("#### 强制例外")[1].split("\n#### ")[0]


def _noise_list_block() -> str:
    """「不报清单」段正文."""
    text = _boundary_text()
    assert "#### 不报清单" in text
    return text.split("#### 不报清单")[1].split("\n#### ")[0]


def test_security_class_is_not_reported():
    """安全类必须进「不报清单」—— 被检对象是内网测试台内部代码, 无外部攻击面.

    Why 需要这条: 需求明确「注入 / 安全这类常规检视在本场景包要降噪」。
    漏了这条 → 模型仍会把命令注入、明文凭据报出来，噪音照旧。
    """
    block = _noise_list_block()
    for needle in ("安全类", "命令", "SQL 注入", "明文凭据", "越权", "路径穿越"):
        assert needle in block, f"不报清单缺少安全类关键词: {needle}"


def test_security_is_not_in_forced_exceptions():
    """安全类**不得**出现在强制例外里 —— 否则又会被拉回来照常报.

    强制例外里保留的是: 数据/硬件损坏、假通过假失败、caller 失配、
    会误导的注释、仓库自定义规则。
    """
    block = _exception_block()
    for forbidden in ("安全漏洞", "命令 / SQL 注入", "明文凭据", "越权"):
        assert forbidden not in block, (
            f"强制例外里仍保留「{forbidden}」—— 与安全类不报清单直接矛盾"
        )
    for required in ("数据损坏", "假通过", "caller", "stale_comment", "仓库自定义规则"):
        assert required in block, f"强制例外丢了「{required}」—— 信号类被误裁"


def test_general_rules_declares_shell_not_reported():
    """规则表必须显式声明 R-SHELL 本场景不报（模型读表时就能看到）."""
    text = (PROFILE_ROOT / "rules" / "general_rules.md").read_text(encoding="utf-8")
    assert "本场景不报" in text
    assert "R-SHELL" in text
    assert "本场景不报" in text.split("| `R-SHELL`")[1].split("\n")[0], (
        "R-SHELL 规则行必须标注「本场景不报」"
    )
    # 硬件数据类不在不报范围
    assert "R-DATA" in text and "仍要照常报" in text


def test_improve_prompt_agrees_on_security_exclusion():
    """improve.md 必须与边界包口径一致: 安全类不报, 数据/硬件/假通过仍要报.

    Why 需要这条: 两处口径一旦打架, 模型会挑对自己「更有说服力」的那套 ——
    结果就是安全类又被报出来（上一轮 N1/N2 就是同类的口径冲突）。
    """
    improve = (PROFILE_ROOT / "prompts" / "improve.md").read_text(encoding="utf-8")
    section = improve.split("## 检视范围与降噪边界")[1].split("\n## ")[0]
    assert "安全类" in section and "不报" in section
    # 「仍要报」不得含安全
    assert "安全 / 数据 / 硬件" not in section
    # 仍要报的清单必须保留数据 / 硬件 / 假通过
    for required in ("数据 / 硬件 / 假通过假失败", "跨文件 caller 失配"):
        assert required in section, f"降噪段丢了「{required}」"
    # 强制要求段不得再把安全类列为「一律产出」
    forced = improve.split("### 🔴 强制要求")[1].split("\n### ")[0]
    assert "安全类例外" in forced
    assert "SQL 注入、可变默认参数" not in forced


def test_suggestion_rationale_must_cite_rule_key():
    """每条 suggestion 的 `rationale` 必须以规则键开头.

    Why 需要这条: 真实业务复验（MR !7）发现**一条建议的 `rule_keys` 落库为空** ——
    模型写了正确的 rationale，但漏了 `R-ERR:` 这类规则键前缀。后果: 周报「问题类型」
    统计、去重指纹、分诊全部归不了类，而 L3 的「规则键引用」检查会直接报红。
    这里锁住提示词里的自检项（写不出键就别报），避免同类问题反复出现。
    """
    improve = (PROFILE_ROOT / "prompts" / "improve.md").read_text(encoding="utf-8")
    assert "首句以规则键开头" in improve
    assert "写不出键就说明这条不该报" in improve
