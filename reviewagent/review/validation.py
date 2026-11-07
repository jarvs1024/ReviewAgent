"""建议校验 / 合并 / 指纹 — 语言无关的共享实现.

从 `commands/improve.py`（`_validate_suggestion` / `_merge_chunks` /
`_suggestion_fingerprint` / `_suggestion_content_fingerprint`）与
`review/runner.py`（`merge_chunks`）迁出的活逻辑；门面
(`ImproveCommand`) 只保留薄委托，语言差异经参数注入
（`find_line` / `read_lines` / `fix_indent` / `first_line_matches`）.

架构边界：本模块**不得** import `reviewagent.review.policies.*` /
`reviewagent.commands.*`；语言实现由调用方（门面 / `PythonPolicy`）注入.
`file_sources` / `line_map` 是请求级缓存，由 runner 持有并原样传入.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from reviewagent.git.diff_lines import find_nearest_valid_line
from reviewagent.logging_setup import logger
from reviewagent.review.policy import rule_ref_pattern
from reviewagent.review.types import FileContext, RiskVerdict, Suggestion


def suggestion_fingerprint(file_path: str, line: int, header: str | None) -> str:
    """Stable fingerprint for suggestion dedup.

    基于 (file:line:header_normalized) 三元组 — 同位置同类问题永远同 fp,
    与 existing_code 行数变化无关 (之前用 sha256(existing_code) 会被 LLM
    给的 1~N 行范围差绕过). lower-case header normalize 让大小写不敏感.

    Args:
        file_path: suggestion 文件路径.
        line: new_line (建议应用的行).
        header: suggestion header (raw, 含大小写/空格都会被 normalize).

    Returns:
        24 字符 hex fingerprint. 与 _suggestion_dedup_key 保持一致.
    """
    hdr_norm = (header or "").strip().lower()
    return hashlib.sha256(
        f"{file_path}:{line}:{hdr_norm}".encode()
    ).hexdigest()[:24]


def suggestion_content_fingerprint(existing_code: str) -> str:
    """基于 existing_code normalize 后的内容指纹 (16 字符 hex).

    Why 不依赖 line: Apply 一个 suggestion 会让后续行号 +N, 同一行代码
    物理位置变化但内容不变 → 行号 fingerprint 失效, 内容 fingerprint 命中.

    normalize: strip + 折叠多空白为单空格. 缩进差异视为同代码 (避免 LLM
    给的 leading-trim 噪声破坏 fingerprint).

    与 suggestion_fingerprint 互补:
      - suggestion_fingerprint: 同 file:line 同类问题 (行号敏感)
      - suggestion_content_fingerprint: 同 existing_code 内容 (行号不敏感)
    """
    import re as _cf_re
    code_norm = _cf_re.sub(r"\s+", " ", (existing_code or "").strip())
    return hashlib.sha256(code_norm.encode()).hexdigest()[:16]


def validate_suggestion(
    sug: Suggestion,
    *,
    ctx: FileContext,
    policy: Any,
    rule_set: Any,
    cfg: Any,
    find_line: Callable[..., int | None],
    file_sources: dict[str, list[str]],
    line_map: dict[str, set[int]],
    project_id: Any = None,
    mr_iid: Any = None,
) -> RiskVerdict:
    """校验一条 suggestion（通用校验顺序见 `validate_fields`）.

    `sug` 的 `file` / `start_line` / `existing_code` / `improved_code` 在此适配；
    语言差异经 `policy` 的三个公共钩子（`read_lines` / `fix_indent` /
    `code_first_line_matches`）注入 — 本模块不 import policies / commands.
    `rule_set` / `cfg` 为后续任务（动态规则 / 配置化校验）保留，当前不使用.
    """
    # 入口守卫: 非法 start_line 直接 drop（bool 是 int 子类，需单独排除），
    # 不进 validate_fields → 不触发惰性读盘 / snap / 对齐检查.
    start_line = sug.get("start_line")
    if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line <= 0:
        return {"action": "drop", "new_line": 0, "reason": "invalid start_line"}

    return validate_fields(
        file_path=sug.get("file") or ctx.file_path,
        start_line=start_line,
        improved_code=sug.get("improved_code") or "",
        existing_code=sug.get("existing_code") or "",
        line_map=line_map,
        file_sources=file_sources,
        find_line=find_line,
        read_lines=policy.read_lines,
        fix_indent=policy.fix_indent,
        first_line_matches=policy.code_first_line_matches,
        project_id=project_id,
        mr_iid=mr_iid,
    )


def validate_fields(
    *,
    file_path: str,
    start_line: int,
    improved_code: str,
    existing_code: str = "",
    line_map: dict[str, set[int]],
    file_sources: dict[str, list[str]],
    find_line: Callable[..., int | None],
    read_lines: Callable[[str], list[str]],
    fix_indent: Callable[[str, str], str],
    first_line_matches: Callable[[str, str], bool],
    project_id: Any = None,
    mr_iid: Any = None,
) -> RiskVerdict:
    """校验 + snap — 返回 {"action": "post"|"drop", "new_line": int, "reason": str}.

    校验顺序:
      1. file 在 diff 中？否则 drop
      2. 优先用 existing_code 反查真实行号（model 经常把 start_line 写错，
         但 existing_code 内容是对的）— 比 snap 准
      3. 反查结果不在 diff valid 集合 → snap 到最近 valid
      4. improved_code 第一行 vs file[start_line-1] 不匹配 → degrade
    """
    valid = line_map.get(file_path)
    # 文件不在 diff 中（agent 乱猜）→ drop
    if valid is None:
        return {"action": "drop", "new_line": start_line,
                "reason": f"file '{file_path}' not in diff"}

    # 预读文件
    if file_path not in file_sources:
        file_sources[file_path] = read_lines(file_path)
    file_lines = file_sources[file_path]

    # 2. 用 existing_code 反查真实行号
    actual_line: int | None = None
    if existing_code and existing_code.strip():
        actual_line = find_line(
            file_lines, existing_code, hint_line=start_line, max_window=8,
        )
        if actual_line is not None and actual_line != start_line:
            logger.info(
                "improve.snap_to_existing project={} mr={} file={} {} -> {} (from existing_code)",
                project_id, mr_iid, file_path, start_line, actual_line,
            )
            start_line = actual_line
        elif actual_line is None and start_line in valid:
            # 反查失败但 start_line 在 valid set 内 — agent 给的 existing_code
            # 在文件里搜不到 (典型: 视图过期 / 行号填错 / 文件已被同步之前改动).
            # 不再走 snap + step 4 对齐 (那会 snap 到 valid 内某行然后对不上 → general,
            # 误以为 "diff 范围"问题). 视为 agent 输出不连贯, 直接 drop.
            logger.warning(
                "improve.existing_code_not_found project={} mr={} file={} "
                "start_line={} hint='{}'",
                project_id, mr_iid, file_path, start_line,
                existing_code[:80].replace("\n", " "),
            )
            return {"action": "drop", "new_line": start_line,
                    "reason": "existing_code not found near start_line in worktree — agent output inconsistent"}

    # 3. snap 到最近 valid（如果上面没改）
    if actual_line is None:
        snapped = find_nearest_valid_line(file_path, start_line, line_map)
        if snapped is None:
            return {"action": "drop", "new_line": start_line,
                    "reason": "no valid line in file"}
        if snapped != start_line:
            logger.info(
                "improve.snap_line project={} mr={} file={} {} -> {}",
                project_id, mr_iid, file_path, start_line, snapped,
            )
            start_line = snapped

    # 3.5 验证 existing_code 与目标行内容匹配 (防止 start_line 与 existing_code 错位)
    #     场景: LLM 给 start_line=278 (execute 行) 但 existing_code 是 L279 的 f-string
    #     结果: GitLab 在 L278 显示 "删除 execute(" 但 improved_code 是 f-string → 内容不匹配
    if existing_code and existing_code.strip():
        # 取 existing_code 第一行作为匹配基准
        existing_first = existing_code.strip("\n").split("\n")[0].strip()
        if existing_first:
            target_line_content = file_lines[start_line - 1].strip() if start_line - 1 < len(file_lines) else ""
            if target_line_content != existing_first:
                # existing_code 第一行与目标行不匹配 — 尝试在目标行附近搜索
                # 搜索范围: start_line ± 3 行
                search_start = max(0, start_line - 4)  # 0-based
                search_end = min(len(file_lines), start_line + 3)
                found_line = None
                for i in range(search_start, search_end):
                    if file_lines[i].strip() == existing_first:
                        found_line = i + 1  # 转 1-based
                        break
                if found_line is not None and found_line != start_line:
                    logger.info(
                        "improve.fix_line_mismatch project={} mr={} file={} {} -> {} (existing_code first line)",
                        project_id, mr_iid, file_path, start_line, found_line,
                    )
                    start_line = found_line
                else:
                    # 找不到匹配 — drop 防止发布错误的 suggestion
                    logger.warning(
                        "improve.existing_code_line_mismatch project={} mr={} file={} "
                        "start_line={} expected='{}' actual='{}'",
                        project_id, mr_iid, file_path, start_line,
                        existing_first[:60], target_line_content[:60],
                    )
                    return {"action": "drop", "new_line": start_line,
                            "reason": "existing_code first line does not match target line content"}

    # 4. improved_code 第一行 vs file[start_line-1] 对齐检查
    if not file_lines or start_line - 1 >= len(file_lines):
        return {"action": "drop", "new_line": start_line,
                "reason": "file content unavailable for alignment check"}

    target_line_raw = file_lines[start_line - 1] if start_line - 1 < len(file_lines) else ""
    target_line = target_line_raw.strip()
    imp_first = (improved_code.splitlines()[0] if improved_code else "").strip()

    # 4a. 多行替换（improved 行数 > existing 行数）→ 第一行可以完全不同
    #     场景: return open(p).read() → with open(p) as f: \n return f.read()
    #     此时第一行是 with 而原行是 return — 这是合法的 "1→N" 替换
    existing_lines = existing_code.strip("\n").split("\n") if existing_code and existing_code.strip() else []
    improved_lines = improved_code.strip("\n").split("\n") if improved_code else []
    is_multi_line_replacement = (
        bool(existing_lines)
        and len(improved_lines) > len(existing_lines)
        and existing_lines[0].strip() == target_line
    )

    if is_multi_line_replacement:
        # 信任模型: existing_code 已通过反查定位
        # suggestion:-0+{len(existing_lines)-1} 精确替换 existing 范围

        # 尾部去重: agent 有时把 existing 之后的文件行也写进 improved_code,
        # 但 -0+N 不会删除那些行, 导致 Apply 后出现重复行.
        # 检测: improved 末尾的 (improved-existing) 行是否与文件后续行一致, 是则裁掉.
        n_added = len(improved_lines) - len(existing_lines)
        if n_added > 0:
            after_start = start_line - 1 + len(existing_lines)  # 0-based
            after_lines = file_lines[after_start:after_start + n_added]
            tail_lines = improved_lines[-n_added:]
            if len(after_lines) == n_added and all(
                t.strip() == a.strip()
                for t, a in zip(tail_lines, after_lines, strict=False)
            ):
                improved_lines = improved_lines[:-n_added]
                improved_code = "\n".join(improved_lines)
                logger.info(
                    "improve.trim_trailing_dup project={} mr={} file={} "
                    "line={} trimmed={}",
                    project_id, mr_iid, file_path,
                    start_line, n_added,
                )

        normalised_code = fix_indent(target_line_raw, improved_code)
        logger.info(
            "improve.multiline_replace project={} mr={} file={} line={} existing_lines={} improved_lines={}",
            project_id, mr_iid, file_path, start_line,
            len(existing_lines), len(improved_lines),
        )
        # 更新 normalised_code 到 normalisation 结果 (在 publish 阶段生效)
        return {"action": "post", "new_line": start_line, "reason": "multi_line_replacement",
                "normalised_code": normalised_code}

    # 4b. 结构重组旁路 (放在 4c 对齐检查之前, 防 with/try 包装被误 drop)
    #     with-wrap / try-wrap 时 improved 第一行 ≠ target_line (with 与 return 不同),
    #     但 improved 引入了新的「块起始行」 (with/try/if/class/def), 是结构重组,
    #     不应走 4c 的「首行对齐」检查, 直接放行到 4d publish.
    #     例: `f = open() / f.write / f.close` → `with open as f: / f.write`
    #         improved=[with..., f.write] < existing=[f=open, f.write, f.close]
    #         improved[0]=`with ...` ≠ target_line=`f = open(...)`, 但有 `with` 块起始.
    _block_open_re = re.compile(r'^\s*(?:with\s|try\s*:|if\s|for\s|while\s|async\s|class\s|def\s)')
    is_structural_refactor = False
    if len(improved_lines) < len(existing_lines) and improved_lines:
        norm_existing_set = {ln.strip() for ln in existing_lines if ln.strip()}
        new_block_openers = [
            ln.strip() for ln in improved_lines
            if ln.strip() and _block_open_re.match(ln.strip())
            and ln.strip() not in norm_existing_set
        ]
        if new_block_openers:
            is_structural_refactor = True
            logger.info(
                "improve.structural_refactor project={} mr={} file={} "
                "line={} existing={} improved={} new_block_openers={}",
                project_id, mr_iid, file_path,
                start_line, len(existing_lines), len(improved_lines), len(new_block_openers),
            )

    # 4c. 对齐检查
    # N→N 等行数替换: existing_code 已在文件中定位 (actual_line 非 None),
    #   agent 有意改写代码 (e.g. print→logger.info), 第一行不需要匹配.
    # 1→1 且 existing_code 未找到: 仍需校验第一行对齐, 防止模型乱发.
    # 例外: improved 为空 (删除整段) → 不需要对齐第一行
    # 例外: 结构重组 (4b 旁路命中) → with/try 包装首行 ≠ target_line, 跳过对齐
    is_same_line_count = bool(existing_lines) and len(improved_lines) == len(existing_lines)
    if not improved_lines:
        pass  # 删除场景, 不需要对齐
    elif is_structural_refactor:
        pass  # 结构重组旁路: 跳过对齐检查, 直接放行
    elif not (is_same_line_count and actual_line is not None):
        if not first_line_matches(target_line, imp_first):
            return {"action": "drop", "new_line": start_line,
                    "reason": f"improved_code first line doesn't match file:{start_line} ({target_line!r} vs {imp_first!r})"}

    # 4d. 收缩检查: M < N 时区分「真删行」与「结构重组」
    #     结构重组 (上面 4b 已识别, 已放行) → publish
    #     真删 (improved ⊂ existing, 没新内容) → general (无 Apply), 防 agent 误删
    # 例外: M == 0 且 existing 非空 → 整段删除 (duplicated_definition / dead_code),
    #     允许多行删除 suggestion 走到 publish 阶段.
    if len(improved_lines) == 0 and existing_lines:
        logger.info(
            "improve.delete_range project={} mr={} file={} "
            "line={} delete_lines={}",
            project_id, mr_iid, file_path,
            start_line, len(existing_lines),
        )
    elif len(improved_lines) < len(existing_lines) and not is_structural_refactor:
        # 走到这里说明: M < N 且 没引入新块起始行 → 真删, 降级 general
        logger.info(
            "improve.shrink_to_general project={} mr={} file={} "
            "line={} existing={} improved={}",
            project_id, mr_iid, file_path,
            start_line, len(existing_lines), len(improved_lines),
        )
        return {"action": "general", "new_line": start_line,
                "reason": f"shrinking suggestion ({len(existing_lines)} -> {len(improved_lines)} lines)"}

    # 4d. 缩进修正: 若 improved_code 第一行缺缩进, 自动补齐
    normalised_code = fix_indent(target_line_raw, improved_code)

    return {"action": "post", "new_line": start_line, "reason": "ok",
            "normalised_code": normalised_code}


def merge_chunks(
    results: list[dict[str, Any]],
    *,
    cfg: Any,
    score_fn: Callable[[dict[str, Any]], int],
    skipped_files: list[str] | None = None,
    file_priorities: dict[str, float] | None = None,
    new_files: set[str] | None = None,
    cap_truncated: list[str] | None = None,
) -> dict[str, Any]:
    """合并多个 chunk 的结果.

    V9 选优: file_priorities 用于 per-file 预算加权 (高优先级文件更多槽位).
    """
    all_suggestions: list[dict[str, Any]] = []
    summaries: list[str] = []

    for r in results:
        if not isinstance(r, dict):
            continue
        suggs = r.get("suggestions") or []
        if isinstance(suggs, list):
            all_suggestions.extend(suggs)
        sm = (r.get("summary_md") or "").strip()
        if sm:
            summaries.append(sm)

    # 去重: 同 file + 同 start_line 只保留 severity 最高的
    seen: dict[str, dict[str, Any]] = {}
    sev_order = {"high": 3, "medium": 2, "low": 1}
    for s in all_suggestions:
        if not isinstance(s, dict):
            continue
        key = f"{s.get('file', '')}:{s.get('start_line', 0)}"
        existing = seen.get(key)
        if existing is None:
            seen[key] = s
        else:
            new_sev = sev_order.get((s.get("severity") or "medium").lower(), 2)
            old_sev = sev_order.get((existing.get("severity") or "medium").lower(), 2)
            if new_sev > old_sev:
                seen[key] = s

    merged_suggestions = list(seen.values())

    # 严重度过滤: 低于 min_severity 的建议不发布 ({prefix}-RULE 引用豁免)
    min_severity = cfg.improve_min_severity
    if min_severity:
        sev_levels = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        min_level = sev_levels.get(min_severity, 0)
        if min_level > 0:
            kept = []
            for s in merged_suggestions:
                rationale = (s.get("rationale") or "")
                if rule_ref_pattern(cfg.rule_key_prefix).search(rationale):
                    kept.append(s)
                    continue
                s_sev = (s.get("severity") or "medium").lower()
                s_level = sev_levels.get(s_sev, 2)
                if s_level >= min_level:
                    kept.append(s)
                else:
                    logger.info(
                        "improve.severity_filter file={} line={} severity={} header={!r}",
                        s.get("file"), s.get("start_line"), s_sev, s.get("header"),
                    )
            merged_suggestions = kept

    # 评分过滤: 低于 min_score 的建议不发布 ({prefix}-RULE 引用豁免)
    min_score = cfg.improve_min_score
    if min_score > 0:
        kept = []
        for s in merged_suggestions:
            rationale = (s.get("rationale") or "")
            if rule_ref_pattern(cfg.rule_key_prefix).search(rationale):
                kept.append(s)
                continue
            sc = score_fn(s)
            if sc >= min_score:
                kept.append(s)
            else:
                logger.info(
                    "improve.score_filter file={} line={} score={} header={!r}",
                    s.get("file"), s.get("start_line"), sc, s.get("header"),
                )
        merged_suggestions = kept

    # V9 选优: (severity, score) 复合键 + priority 加权 per-file 预算 + 全局两遍填充(覆盖优先)
    # 修复: 原来只按 severity 3 档排序, 同档内随机(稳定排序=处理序) → 50文件时 top15
    # 全落前3个文件, 47文件零覆盖. 改为 score 区分同档 + 每文件先取1条保证覆盖.
    sev_rank = {"high": 3, "medium": 2, "low": 1}

    def _key(s):
        # severity 为主键 (遵循 high/medium/low 严格分级), score 为同档内区分器
        return (sev_rank.get((s.get("severity") or "medium").lower(), 2),
                score_fn(s))

    # per-file 预算: 按 priority 加权 (高优先级文件更多槽位, 低优先级少占, 下限 min_per_file)
    max_per_file = cfg.improve_max_suggestions_per_file
    min_per_file = cfg.improve_min_suggestions_per_file
    _max_prio = max((file_priorities or {}).values()) if file_priorities else 0.0

    def _budget(fp: str) -> int:
        if max_per_file <= 0:
            return 10 ** 9  # 不限
        # A: 新增文件给满额, 不被低优先级压缩 + 豁免单文件截断
        if new_files and fp in new_files:
            return max_per_file
        if not file_priorities or _max_prio <= 0:
            return max_per_file
        norm = (file_priorities.get(fp, 0.0) or 0.0) / _max_prio
        b = min_per_file + round((max_per_file - min_per_file) * norm)
        return max(min_per_file, min(max_per_file, b))

    by_file: dict[str, list[dict[str, Any]]] = {}
    for s in merged_suggestions:
        fp = s.get("file", "") or "_unknown"
        by_file.setdefault(fp, []).append(s)

    # 每文件按复合键留 top budget(fp), 其余计入 per_file_truncated
    per_file_truncated = 0
    kept_list: list[dict[str, Any]] = []
    for fp, suggs in by_file.items():
        suggs.sort(key=_key, reverse=True)
        b = _budget(fp)
        if len(suggs) > b:
            per_file_truncated += len(suggs) - b
            suggs = suggs[:b]
        kept_list.extend(suggs)
    if per_file_truncated > 0:
        logger.info(
            "improve.per_file_limit max_per_file={} min={} truncated={} (降为总览文字)",
            max_per_file, min_per_file, per_file_truncated,
        )
    merged_suggestions = kept_list

    # 全局限流: 两遍填充 — 第1遍每文件1条(覆盖优先), 第2遍按复合键填剩余(质量优先)
    max_suggestions = cfg.improve_max_suggestions
    truncated_count = 0
    if max_suggestions > 0 and len(merged_suggestions) > max_suggestions:
        by_file2: dict[str, list[dict[str, Any]]] = {}
        for s in merged_suggestions:
            fp = s.get("file", "") or "_unknown"
            by_file2.setdefault(fp, []).append(s)
        # 文件按其最佳建议的复合键降序 (高价值文件先占覆盖槽); 新增文件优先 (A: 豁免截断)
        file_order = sorted(by_file2.keys(),
                            key=lambda fp: (1 if (new_files and fp in new_files) else 0,
                                           _key(by_file2[fp][0])),
                            reverse=True)
        phase1: list[dict[str, Any]] = []
        used: dict[str, int] = {}
        for fp in file_order:
            if len(phase1) >= max_suggestions:
                break
            phase1.append(by_file2[fp][0])  # 每文件1条 → 覆盖最大化
            used[fp] = 1
        # 第2遍: 剩余候选按复合键降序填, 受 budget 约束
        remaining = [s for fp in by_file2 for s in by_file2[fp][1:]]
        remaining.sort(key=_key, reverse=True)
        for s in remaining:
            if len(phase1) >= max_suggestions:
                break
            fp = s.get("file", "") or "_unknown"
            if used.get(fp, 0) < _budget(fp):
                phase1.append(s)
                used[fp] = used.get(fp, 0) + 1
        truncated_count = len(merged_suggestions) - len(phase1)
        merged_suggestions = phase1
        logger.info(
            "improve.suggestion_limit kept={} truncated={} files_covered={}",
            max_suggestions, truncated_count,
            len({s.get("file") for s in phase1}),
        )

    # summary: 从 suggestions 列表生成概览
    total_suggestions = len(merged_suggestions)
    if total_suggestions > 0:
        items: list[str] = []
        for s in merged_suggestions:
            fp = s.get("file", "")
            line = s.get("start_line", "")
            header = (s.get("header") or "").strip()
            severity = (s.get("severity") or "medium").upper()
            label = s.get("label", "")
            rationale = (s.get("rationale") or "").strip()
            # 简短版: 文件 + 行号 + 标签 + header + 理由首句
            short = rationale.split("。")[0].split("，")[0]
            line_str = f"L{line}" if line else ""
            items.append(
                f"- **`{fp}`**{line_str} — **{header}** [{severity}/{label}]: {short}"
            )
        merged_summary = (
            "## 改进总览\n\n"
            + "\n".join(items)
        )
        if truncated_count > 0:
            merged_summary += f"\n\n> ℹ️ 另有 {truncated_count} 条低优先级建议未展示（上限 {max_suggestions} 条）"
        if per_file_truncated > 0:
            merged_summary += f"\n\n> ℹ️ 另有 {per_file_truncated} 条建议因单文件上限（{max_per_file} 条/文件）降为总览文字"
        if skipped_files:
            # V6: skipped_files 现在仅含测试/配置文件 (IMPROVE_FULL_FILES 软阈值化后不再丢文件)
            display = skipped_files[:10]
            suffix = f" 等 {len(skipped_files)} 个" if len(skipped_files) > 10 else ""
            merged_summary += (
                f"\n\n> ⏭️ 以下 {len(skipped_files)} 个测试/配置文件跳过检视 (含 IMPROVE_SKIP_TEST_PATHS 配置跳过 + IMPROVE_TEST_SAMPLE_PATHS 抽样跳过): "
                f"{', '.join(display)}{suffix}"
            )
    else:
        merged_summary = "## 改进总览\n\n未发现问题。"
        if skipped_files:
            display = skipped_files[:10]
            suffix = f" 等 {len(skipped_files)} 个" if len(skipped_files) > 10 else ""
            merged_summary += (
                f"\n\n> ⏭️ 以下 {len(skipped_files)} 个测试/配置文件跳过检视: "
                f"{', '.join(display)}{suffix}"
            )

    # D: 文件数硬上限截断的低优先级文件 (与 test_skipped 分开提示)
    if cap_truncated:
        _display = cap_truncated[:10]
        _suffix = f" 等 {len(cap_truncated)} 个" if len(cap_truncated) > 10 else ""
        merged_summary += (
            f"\n\n> ✂️ 另有 {len(cap_truncated)} 个低优先级文件因超出文件数上限"
            f"（IMPROVE_MAX_FILES_TO_REVIEW={cfg.improve_max_files_to_review}）未检视: "
            f"{', '.join(_display)}{_suffix}"
        )

    return {
        "summary_md": merged_summary,
        "suggestions": merged_suggestions,
    }


__all__ = [
    "merge_chunks",
    "suggestion_content_fingerprint",
    "suggestion_fingerprint",
    "validate_fields",
    "validate_suggestion",
]
