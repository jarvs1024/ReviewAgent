"""/improve 命令端到端.

工作流:
    1. 调 opencode agent `improve`
    2. 解析 agent 返回 {summary_md, suggestions[]}
    3. 顶层 summary 评论
    4. 每条 suggestion 作为可 Apply 的 inline comment 发出
       GitLab UI 会渲染代码块 + "Apply suggestion" 按钮，让 reviewer 一键 commit.

格式说明:
    GitLab "committable suggestion" 的 body 必须是 markdown 代码块，\
    其中第一行为 ```suggestion 语言:<lang> 头（与 pr-agent 一致）.
    例:
        ```suggestion:-0
        def f():
            return 1
        ```
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from reviewagent.commands._common import BaseCommand, BaseCommandError
import subprocess
from reviewagent.config import config
from reviewagent.git.diff_lines import (
    find_nearest_valid_line,
    format_line_map_for_prompt,
    parse_diff_line_map,
)
from reviewagent.gitlab.client import GitLabError
from reviewagent.logging_setup import logger
from reviewagent.opencode.client import OpencodeOutputError, client as opencode


# Backward-compat re-exports
ImproveError = BaseCommandError

# 规则引用正则: 匹配 SSD-RULE-XXX 形式 (rule_key_prefix 可配)
# 规则键豁免正则: SSD-RULE-* / R-XXX / R-OTHER:* / R-OTHER-IMPACT:* 全部豁免 _score_suggestion 过滤
_RULE_REF_REGEX = re.compile(
    r"\b(?:SSD-RULE-\w+"
    r"|R-OTHER-IMPACT:[a-z0-9_]+"
    r"|R-OTHER:[a-z0-9_]+"
    r"|R-[A-Z]+(?:-[A-Z0-9_]+)*)\b"
)




class ImproveCommand(BaseCommand):
    COMMAND_NAME = "improve"
    DEFAULT_AGENT = "improve"

    # 每条 suggestion 末尾追加 /adopt /dismiss 帮助文本 (与 pr-agent 一致)
    HELP_TEXT_FOOTER = (
        "\n\n✅ 接受建议\n"
        "   • 直接用：点上方「应用建议」按钮\n"
        "   • 自己改：请先提交修改，再回复 `/adopt [理由]`\n"
        "\n❌ 关闭建议\n"
        "   • 回复 `/dismiss [理由]`\n"
        "\n理由会被记录，用于改进后续建议。"
    )
    # ---------- 并行分块调用 ----------
    def _call_agent(self, ws) -> dict[str, Any]:
        """覆盖基类: 按文件分块 + 并行调 opencode + 合并结果."""
        line_map = self._diff_line_map()
        all_files = sorted(line_map.keys())

        # 文件扩展名过滤: 跳过 .md/.doc/.png 等非代码文件
        excluded_ext = set(config.review_exclude_extensions)
        if excluded_ext:
            code_files = [f for f in all_files if not any(f.lower().endswith(ext) for ext in excluded_ext)]
            if len(code_files) < len(all_files):
                skipped_ext = set(all_files) - set(code_files)
                logger.info(
                    "improve.file_filter project={} mr={} skipped={}",
                    self.project_id, self.mr_iid, sorted(skipped_ext),
                )
            all_files = code_files

        if not all_files:
            return {"summary_md": "## 改进总览\n\n无代码文件变更，跳过检视。", "suggestions": []}

        # 文件数限流: 超出上限的文件跳过，在总览中注明
        max_files = config.improve_max_files
        if max_files > 0 and len(all_files) > max_files:
            files = all_files[:max_files]
            skipped_files = all_files[max_files:]
            logger.info(
                "improve.file_limit project={} mr={} total={} kept={} skipped={}",
                self.project_id, self.mr_iid, len(all_files), len(files), len(skipped_files),
            )
        else:
            files = all_files
            skipped_files = []

        if len(files) <= 1 and not skipped_files:
            return super()._call_agent(ws)  # 单文件走原路径

        # 按文件拆分 diff
        diff_by_file = self._split_diff_by_file(ws.diff_file, files)

        # 并行调用
        workers = min(len(files), config.improve_parallel_workers)
        logger.info(
            "improve.parallel project={} mr={} files={} workers={}",
            self.project_id, self.mr_iid, len(files), workers,
        )

        chunk_results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for fp in files:
                file_diff = diff_by_file.get(fp, "")
                valid_lines = line_map.get(fp, set())
                prompt = self._build_chunk_prompt(fp, file_diff, valid_lines, ws)
                fut = pool.submit(self._call_chunk, prompt, ws, fp)
                futures[fut] = fp
            for fut in as_completed(futures):
                fp = futures[fut]
                try:
                    result = fut.result()
                    chunk_results.append(result)
                except Exception as e:
                    logger.error(
                        "improve.chunk_failed project={} mr={} file={} err={}",
                        self.project_id, self.mr_iid, fp, e,
                    )
                    # 单个 chunk 失败不影响其他
                    chunk_results.append({"summary_md": "", "suggestions": []})

        return self._merge_chunks(chunk_results, skipped_files=skipped_files)

    def _call_chunk(self, prompt: str, ws, file_path: str) -> dict[str, Any]:
        """单个 chunk 的 opencode 调用."""
        logger.info(
            "improve.chunk_start project={} mr={} file={}",
            self.project_id, self.mr_iid, file_path,
        )
        oc_result = opencode.run(
            agent=self.DEFAULT_AGENT,
            prompt=prompt,
            workdir=ws.worktree,
            files=[],  # 不内联文件，prompt 里已包含 diff
            timeout=config.rq_worker_timeout,
        )
        # 记录最后一个成功的结果 (token 统计)
        self._last_oc_result = oc_result
        logger.info(
            "improve.chunk_done project={} mr={} file={} tokens_in={} tokens_out={}",
            self.project_id, self.mr_iid, file_path,
            oc_result.prompt_tokens, oc_result.completion_tokens,
        )
        return oc_result.data

    @staticmethod
    def _split_diff_by_file(diff_file: Path, files: list[str]) -> dict[str, str]:
        """解析 unified diff，按文件拆分. 返回 {file_path: diff_text}."""
        try:
            full_diff = diff_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        result: dict[str, str] = {}
        # 按 "diff --git" 分割
        parts = re.split(r"(?=^diff --git )", full_diff, flags=re.MULTILINE)
        for part in parts:
            if not part.strip():
                continue
            # 提取文件路径: "diff --git a/xxx b/xxx"
            m = re.match(r"diff --git a/.+ b/(.+)", part)
            if m:
                fp = m.group(1).strip()
                result[fp] = part
        return result

    @staticmethod
    def _extract_identifiers(diff: str) -> list[str]:
        """从 diff 的 `+` 行提取改动的标识符 (def/class/常量/import 名字)."""
        if not diff:
            return []
        # 只看 + 行
        added = "\n".join(ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        idents: set[str] = set()
        for m in re.finditer(r"^\s*def\s+([A-Za-z_]\w{2,})\s*\(", added, re.MULTILINE):
            idents.add(m.group(1))
        for m in re.finditer(r"^\s*class\s+([A-Za-z_]\w{2,})", added, re.MULTILINE):
            idents.add(m.group(1))
        for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]{2,})\s*[:=]", added, re.MULTILINE):
            idents.add(m.group(1))
        for m in re.finditer(r"^\s*from\s+\S+\s+import\s+([A-Za-z_]\w{2,})", added, re.MULTILINE):
            idents.add(m.group(1))
        for m in re.finditer(r"^\s*from\s+\S+\s+import\s+\(([^)]+)\)", added, re.MULTILINE):
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip()
                if len(name) >= 3:
                    idents.add(name)
        # 排除常见噪音
        stop = {"self", "cls", "None", "True", "False", "args", "kwargs"}
        return sorted(idents - stop)[:15]

    @staticmethod
    def _find_cross_file_refs(file_path: str, diff: str, worktree_path, max_refs: int = 6) -> list[dict]:
        """在 worktree 找 diff 改动的标识符在其他文件的 caller 引用.

        用 rg (ripgrep) 排除当前文件, 每个 ident 限 2 条命中, 总数限 max_refs.
        返回 [{"file": rel, "line": N, "content": "..."}, ...]
        """
        from pathlib import Path
        idents = ImproveCommand._extract_identifiers(diff)
        if not idents or not worktree_path:
            return []
        workdir = str(Path(worktree_path))
        refs: list[dict] = []
        seen: set[tuple[str, int]] = set()
        rel_self = file_path
        for ident in idents:
            if len(ident) < 3:
                continue
            try:
                result = subprocess.run(
                    ["rg", "-n", "--no-heading", "-t", "py",
                     rf"\b{ident}\b", ".",
                     "-g", f"!{rel_self}",
                     "-g", "!reviewagent/**",
                     "-g", "!tests/**",
                     "-g", "!.venv/**",
                     "-g", "!**/__pycache__/**",
                     "-g", "!.git/**"],
                    cwd=workdir,
                    capture_output=True, text=True, timeout=8,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                continue
            hits = 0
            for line in result.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                f, ln_s, content = parts[0], parts[1], parts[2]
                f_rel = f.lstrip("./")
                if f_rel == rel_self or not f_rel:
                    continue
                try:
                    ln = int(ln_s)
                except ValueError:
                    continue
                key = (f_rel, ln)
                if key in seen:
                    continue
                seen.add(key)
                refs.append({
                    "file": f_rel,
                    "line": ln,
                    "content": content.strip()[:500],
                    "ident": ident,
                })
                hits += 1
                if hits >= 2 or len(refs) >= max_refs:
                    break
            if len(refs) >= max_refs:
                break
        return refs

    def _render_cross_file_section(self, refs: list[dict]) -> str:
        """把 cross-file 引用渲染成 chunk prompt 段."""
        if not refs:
            return (
                "## 🔍 跨文件影响分析 (规则检查之前必做, **P1**)\n\n"
                "Python 端 rg 没找到 cross-file caller 引用 (本文件可能是新增 / 改动是局部 / 仓库无其他文件引用).\n\n"
                "**仍请按 P1 优先级自行判断是否需要产 R-OTHER-IMPACT suggestion** — Python 端仅做粗扫，"
                "如下情况仍可能有跨文件风险:\n"
                "- 新增 / 重命名的公共函数、类、常量 (无 caller 但下游模块可能依赖)\n"
                "- 改了 fixture / 测试 helper (本 diff 未引用但其它 test 可能引用)\n"
                "- 改了 import 路径 (旧路径可能还有引用未被发现)\n"
                "- 改了 SQL / ORM schema (model/migration 可能未同步)\n\n"
                "确认无风险 → 在 summary_md 注明 '未发现 cross-file 关联'。**不要为凑数硬编 R-OTHER-IMPACT**。\n\n"
            )
        section = "## 🔍 跨文件影响分析 (Python 端已 grep, **优先级 P1, 在所有规则检查之前**)\n\n"
        section += f"在 worktree 找到 {len(refs)} 条其他文件对本文件改动的引用:\n\n"
        for ref in refs:
            section += (
                f"**`{ref['file']}:{ref['line']}`** 引用 `{ref['ident']}`:\n"
                f"```\n{ref['content']}\n```\n\n"
            )
        section += (
            "**请逐一分析每条 caller 是否需要同步更新**：\n"
            "- 函数签名变了但 caller 没传新参数 / 类型不匹配 → 产 suggestion, `label: cross-file impact`\n"
            "- 常量改了引用方没同步 → 产 suggestion, `label: cross-file impact`\n"
            "- 删除/重命名的函数别处还在用 → 写进 summary_md 文字, 格式 `> 跨文件影响: <文件> L<行号> <问题>`\n"
            "- 跨文件影响类问题**不要求先命中 R-XXX 19 类**, 命中即产 suggestion\n"
            "- `rationale` 必须以 `R-OTHER-IMPACT:<简短描述>` 开头 (例 `R-OTHER-IMPACT:caller_param` / `R-OTHER-IMPACT:schema_drift` / `R-OTHER-IMPACT:import_path` / `R-OTHER-IMPACT:fixture_break`)\n"
            "- **严格只用于跨文件影响** — 同文件内的资源/异常/循环问题应归 R-RES / R-LOOP / R-ERR 等, 不要错归 R-OTHER-IMPACT\n\n"
        )
        return section

    def _build_chunk_prompt(
        self, file_path: str, file_diff: str, valid_lines: set[int], ws
    ) -> str:
        """构建单文件的精简 prompt."""
        wt = str(ws.worktree)

        # 读取完整源码 (限制最大行数, 超过截断 + 提示)
        _MAX_SOURCE_LINES = 2500
        lines = self._read_file_lines(file_path)
        if lines:
            total_lines = len(lines)
            if total_lines > _MAX_SOURCE_LINES:
                # 截断到前 _MAX_SOURCE_LINES 行, 提示末尾未加载
                kept_lines = lines[:_MAX_SOURCE_LINES]
                numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(kept_lines))
                source_block = (
                    f"### 完整源码: `{file_path}` (共 {total_lines} 行, **已截断到前 {_MAX_SOURCE_LINES} 行**)\n"
                    f"⚠️ 上下文不完整 — 末尾 {total_lines - _MAX_SOURCE_LINES} 行未加载, "
                    f"如需检视请缩小 diff 范围或拆分 MR\n```\n{numbered}\n```"
                )
            else:
                numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(lines))
                source_block = f"### 完整源码: `{file_path}` (共 {len(lines)} 行)\n```\n{numbered}\n```"
        else:
            source_block = f"### 完整源码\n(无法读取 {file_path})"


        # VALID NEW LINES
        vl_sorted = sorted(valid_lines)
        vl_str = f"{file_path}: {vl_sorted}"

        # 通用规则清单 + 仓库规则 — 与 system prompt 保持一致
        rules_block = ""
        if self.repo_context:
            rules_block = (
                f"## 🔴 优先 1 — SSD 自定义规则 (项目方定义, 最高优先级)\n\n"
                f"先扫下面 SSD 规则命中, 命中即产 suggestion, rationale 引用规则键:\n\n"
                f"{self.repo_context}\n\n"
                f"## 🟡 优先 2 — 通用规则 (常规代码 + 测试代码问题, 共 19 条 R-XXX + R-OTHER 兜底)\n\n"
                f"完成 SSD 规则扫描后, 再用以下通用规则覆盖剩余问题, rationale 以 R-XXX 开头:\n\n"
                f"- R-REPRO: time.time()/datetime.now() 在测试函数 → time.perf_counter()/mock\n"
                f"- R-RES: open/serial/socket/Popen/ioctl 创建后无 with 或 close/wait → 资源用 with 或 try/finally close\n"
                f"- R-TIME: time.sleep(N) 写死 / 阻塞 IO 缺 timeout= / while True 忙循环无 max_retry → poll-with-timeout / 加 timeout= / 设 max_retry\n"
                f"- R-ASSERT: assert 缺 msg / 顺序反 / assertEqual(x, None) / silent test (无 assert) → 加 msg / assertIs / 必加 assert\n"
                f"- R-FIX: setUp 无 tearDown / open(/tmp/x) 不用 tmp_path / 临时资源无 try/finally → 配对 teardown / 用 tmp_path / try/finally 兜底\n"
                f"- R-SKIP: @skipIf 缺 reason= / 平台判断写死 / 假设 root 或设备路径无 try → 加 reason / fallback / try 兜底\n"
                f"- R-ERR: except: pass 静默吞错 / traceback.print_exc() 替代 logger → 捕获具体异常 + logger.exception\n"
                f"- R-LOG: print() 替代 logger / 测试失败不 dump 设备上下文 (dmesg/smartctl/nvme list) → 改 logger.* / 失败时 dump 状态\n"
                f"- R-CI: 多 test 共写同一路径 (race) / 需独占设备无 @pytest.mark.serial 或 lock → tmp_path + PID 后缀 / 加 serial 标记或 lock\n"
                f"- R-NVME: NVMe/SCSI struct.pack 字节序错 (<I vs >I) / opcode 硬编码 / buffer length 跟 sector size 不匹配 / 超时未 RESPONSE abort → 命名常量 / 对齐字节序 / 匹配 sector / 超时 abort\n"
                f"- R-PERF: 短操作 (< 1ms) 用 time.time() 而非 time.perf_counter() / 测量区间过大 → 改 perf_counter / 收紧区间\n"
                f"- R-CONST: 超时 / 重试 / 固件地址 / 服务地址硬编码 / 配置读取无校验 → 收拢常量 + 配置多层校验\n"
                f"- R-SHELL: subprocess f-string 拼外部参数透传设备入参 → args 列表 + 白名单过滤\n"
                f"- R-LOOP: 无限 while 无 max_retry / 截止 deadline / 遍历原地修改容器 → 重试 + deadline / 副本遍历\n"
                f"- R-MEM: 全盘数据 / 海量日志全量加载内存 / 大对象频繁创建无释放 → 流式读写 / 缩小生命周期\n"
                f"- R-LOCK: 多线程 / 多进程并发操作 SSD 无锁 / 异常崩溃锁长期持有 → 文件锁 / finally 释放\n"
                f"- R-DEP: 三方依赖无版本锁定 / 强绑系统命令无多系统兜底 → 固化版本 + 可用性检测\n"
                f"- R-STATE: 下发 NVMe 指令 / 磁盘读写前未校验设备在位 / 健康 / 固件就绪 → 前置校验 + 异常终止\n"
                f"- R-DATA: SSD 载荷 / LBA / 扇区长度裸透传无值域 / 对齐 / 长度校验 → 报文前置合法性校验\n"
                f"- R-CLEAN: 用例异常残留挂载点 / 裸盘占用 / 临时镜像 / 垃圾堆积 → 统一后置清理 + 兜底回收\n\n"
                f"### 兜底: 野生问题 (不在上面 19 条 R-XXX 内但有价值的)\n"
                f"仅当 high severity (潜在 bug / 安全隐患) 才强制产 suggestion, rationale 以 `R-OTHER:<简短描述>` 开头, 例:\n"
                f"- `R-OTHER:magic_number` 硬编码魔法数 (端口/超时/重试等)\n"
                f"- `R-OTHER:typo` 标识符 / 注释拼写错误\n"
                f"- `R-OTHER:dead_code` diff 引入后立即未使用\n"
                f"- `R-OTHER:naming_inconsistency` 与同模块命名风格不一致\n"
                f"- `R-OTHER:duplicated_definition` 与已有常量 / 函数重复\n"
                f"- `R-OTHER:stale_comment` 注释与代码实际行为已不一致\n"
                f"rationale 末尾加一句 '未命中 R-XXX 19 类, 原因: ...', 真的没找到就空着, 不要硬编.\n\n"
                f"命中不要求必给 suggestion, 只在能直接 Apply 时才给 (无法 Apply → summary_md 文字描述).\n\n"
            )

        # Python 端预先用 rg 找 cross-file caller 引用, 渲染成 prompt 段 (放在规则清单前 — 跨文件影响类问题不要求命中 R-XXX)
        cross_file_refs = self._find_cross_file_refs(file_path, file_diff, wt)
        cross_file_section = self._render_cross_file_section(cross_file_refs)

        return (
            f"{cross_file_section}"
            f"{rules_block}"
            f"## 本次检视文件: `{file_path}`\n\n"
            f"### diff\n```diff\n{file_diff}\n```\n\n"
            f"{source_block}\n\n"
            f"### VALID NEW LINES（start_line 只能从此取）\n\n{vl_str}\n\n"
            f"## 输出\n\n"
            f"按 system prompt 输出 JSON。"
            f"**summary_md 输出空字符串 `\"\"`**，只输出本文件的 suggestions。"
        )

    @staticmethod
    def _merge_chunks(results: list[dict[str, Any]], *, skipped_files: list[str] | None = None) -> dict[str, Any]:
        """合并多个 chunk 的结果."""
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

        # 评分过滤: 低于 min_score 的建议不发布 (SSD-RULE 引用豁免)
        min_score = config.improve_min_score
        if min_score > 0:
            kept = []
            for s in merged_suggestions:
                rationale = (s.get("rationale") or "")
                if _RULE_REF_REGEX.search(rationale):
                    kept.append(s)
                    continue
                sc = ImproveCommand._score_suggestion(s)
                if sc >= min_score:
                    kept.append(s)
                else:
                    logger.info(
                        "improve.score_filter file={} line={} score={} header={!r}",
                        s.get("file"), s.get("start_line"), sc, s.get("header"),
                    )
            merged_suggestions = kept

        # 建议数限流: 按 severity 排序，保留前 N 条，超出只写总览
        max_suggestions = config.improve_max_suggestions
        truncated_count = 0
        if max_suggestions > 0 and len(merged_suggestions) > max_suggestions:
            sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
            merged_suggestions.sort(
                key=lambda s: sev_rank.get((s.get("severity") or "medium").lower(), 2),
                reverse=True,
            )
            truncated_count = len(merged_suggestions) - max_suggestions
            merged_suggestions = merged_suggestions[:max_suggestions]
            logger.info(
                "improve.suggestion_limit kept={} truncated={}",
                max_suggestions, truncated_count,
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
            if skipped_files:
                merged_summary += f"\n\n> ⚠️ 以下文件因数量超限未检视: {', '.join(skipped_files)}"
        else:
            merged_summary = "## 改进总览\n\n未发现问题。"

        return {
            "summary_md": merged_summary,
            "suggestions": merged_suggestions,
        }

    # ---------- helpers ----------
    def _get_mr_head_sha(self) -> str | None:
        """获取当前 MR 的 head_sha (用于 record_suggestion 时记录发布 SHA)."""
        try:
            refs = self.gitlab.get_mr_diff_refs(self.project_id, self.mr_iid)
            return refs.get("head_sha") or refs.get("start_sha")
        except Exception as e:
            logger.warning("improve.get_mr_head_sha failed: {}", e)
            return None

    def _diff_line_map(self) -> dict[str, set[int]]:
        """读 self.ws.diff_file 解析每个文件的 valid new_line 集合."""
        if not self.ws or not self.ws.diff_file:
            return {}
        try:
            diff_text = Path(self.ws.diff_file).read_text(encoding="utf-8")
        except OSError:
            return {}
        return parse_diff_line_map(diff_text)

    def _read_file_lines(self, file_path: str) -> list[str]:
        """从 worktree 读 file 源，按行 split；读不到返回 []."""
        if not self.ws:
            return []
        # 绝对路径 / 相对路径都兼容
        ws_root = Path(self.ws.worktree)
        candidates = [ws_root / file_path]
        # 若 file_path 含 ../ 等相对引用，从 worktree 解析
        try:
            candidates.append((ws_root / file_path).resolve())
        except OSError:
            pass
        for p in candidates:
            try:
                if p.is_file():
                    return p.read_text(encoding="utf-8").splitlines(keepends=False)
            except OSError:
                continue
        return []

    @staticmethod
    def _find_line_by_existing_code(
        file_lines: list[str],
        existing_code: str,
        *,
        hint_line: int = 0,
        max_window: int = 5,
    ) -> int | None:
        """在 file_lines 中搜索 existing_code 块，返回匹配首行行号（1-based）.

        匹配策略（按优先级）:
          1. 从 hint_line 附近 ±max_window 找最接近的完整块匹配
          2. 整文件从头到尾找第一个完整块匹配
          3. 找首行精确匹配（fallback — 处理 existing_code 只有一行的情况）

        返回 None 表示没找到.
        """
        if not file_lines or not existing_code.strip():
            return None
        target_lines = existing_code.strip("\n").split("\n")
        target_first = target_lines[0].strip()
        if not target_first:
            return None

        n = len(file_lines)
        m = len(target_lines)

        def _block_matches_at(start_idx: int) -> bool:
            if start_idx + m > n:
                return False
            for j in range(m):
                if file_lines[start_idx + j].strip() != target_lines[j].strip():
                    return False
            return True

        # 1. hint_line 附近（±max_window）
        if hint_line >= 1:
            lo = max(1, hint_line - max_window)
            hi = min(n - m + 1, hint_line + max_window)
            best: int | None = None
            best_dist = max_window + 1
            for s in range(lo, hi + 1):
                if _block_matches_at(s - 1):
                    dist = abs(s - hint_line)
                    if dist < best_dist:
                        best = s
                        best_dist = dist
            if best is not None:
                return best

        # 2. 整文件从头找
        for s in range(1, n - m + 2):
            if _block_matches_at(s - 1):
                return s

        # 3. 首行精确匹配（fallback）
        for i, line in enumerate(file_lines, start=1):
            if line.strip() == target_first:
                return i
        return None

    def _build_user_prompt(self) -> str:
        """把 diff 的 valid new_line 集合 + 完整文件源码喂给 agent —
        严格约束它的 start_line 取值，让模型能精确数出文件行号。"""
        line_map = self._diff_line_map()

        # 仓库规则上下文 (AGENTS.md) — 优先遵循
        repo_ctx_block = ""
        if self.repo_context:
            repo_ctx_block = (
                "## 仓库规则 (AGENTS.md) — 优先遵循\n\n"
                "以下是本仓库的编码规范 / 检视规则。"
                "**你的建议必须优先覆盖这些规则**：如果 diff 中存在违反下列规则的地方，"
                "必须给出对应的 inline suggestion。\n\n"
                f"{self.repo_context}\n\n"
            )

        # === C. 已发过的建议列表 — 让 agent 知道自己说过了 ===
        # 这样 agent 看到 diff 时, 如果是同样问题就 skip, 不会重复发
        already_suggested_block = ""
        try:
            from reviewagent.telemetry.store import get_store
            existing = get_store().list_suggestion_headers(
                self.project_id, self.mr_iid
            )
            if existing:
                items = []
                for s in existing[:50]:  # 限制 50 条避免 prompt 过长
                    fp = s.get("fp_short", "?")
                    sev = (s.get("severity") or "?").upper()
                    st = (s.get("status") or "?").upper()
                    fp_path = s.get("file_path", "?")
                    line = s.get("target_line", "?")
                    hdr = (s.get("header") or "?").strip()[:30]
                    items.append(
                        f"- `{fp_path}` L{line} — {hdr} [{sev}/{st}] (fp={fp})"
                    )
                already_suggested_block = (
                    "## ⚠️ 已发过的建议（不要重复）\n\n"
                    f"本 MR 已发布 **{len(existing)}** 条 suggestion. "
                    "**绝对不要重复这些**，即使代码再次出现也一样.\n"
                    "如果你认为某条已 applied / 关闭但应该重新评估，"
                    "**只在 summary_md 里文字描述**，不要发新 suggestion。\n\n"
                    + "\n".join(items)
                    + "\n\n"
                )
        except Exception as e:
            logger.warning("improve.list_existing failed (non-fatal): {}", e)

        if not line_map:
            return (
                f"{repo_ctx_block}"
                f"{already_suggested_block}"
                "请按你的 system prompt 处理当前 MR 的 diff"
                "（变更内容见上方附件文件）。"
            )
        formatted = format_line_map_for_prompt(line_map)

        # 收集每个文件的源码（带 `<行号>: ` 前缀，方便模型精确数行）
        file_blocks: list[str] = []
        for fp in sorted(line_map.keys()):
            lines = self._read_file_lines(fp)
            if not lines:
                continue
            numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(lines))
            file_blocks.append(
                f"### 完整源码：`{fp}`（共 {len(lines)} 行；行号在左侧）\n```\n{numbered}\n```"
            )
        files_text = "\n\n".join(file_blocks) if file_blocks else "(no files)"

        return (
            f"{repo_ctx_block}"
            "请按你的 system prompt 处理当前 MR 的 diff\n"
            "（变更内容见上方附件文件）。\n\n"
            "## diff 有效新增行（VALID NEW LINES）\n\n"
            "下面列出本次 diff 里每个文件所有以 `+` 开头的**新文件行号**。"
            "**你的 `start_line` 必须且只能从此集合中取**；"
            "若你怀疑某 issue 的目标行不在此集合里（context 行 / 删除行 / "
            "跨文件推断），请放弃 `improved_code`，改为在 `summary_md` 里文字描述，"
            "不要强行填一个错位的 suggestion。\n\n"
            f"{formatted}\n\n"
            "## 完整文件源码（带行号）\n\n"
            "**强烈建议**：每条 suggestion 的 `start_line` 必须**精确等于** "
            "下方源码里 `existing_code` 第一行对应的行号。"
            "Python 端会用 `existing_code` 反查行号校验 — 行号错位的会被自动降级。\n\n"
            f"{files_text}\n"
        )

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        summary_md = (agent_result.get("summary_md") or "").strip()
        suggestions = agent_result.get("suggestions") or []
        if not isinstance(suggestions, list):
            raise OpencodeOutputError(
                f"agent output 'suggestions' must be list, got {type(suggestions).__name__}"
            )

        line_map = self._diff_line_map()
        file_sources: dict[str, list[str]] = {}

        # 1. 顶层 summary
        top_comment_id: int | None = None
        if summary_md:
            try:
                top_comment_id = self.gitlab.post_mr_comment(
                    self.project_id, self.mr_iid, summary_md
                )
            except GitLabError as e:
                raise BaseCommandError(f"post summary comment failed: {e}") from e

        # 2. 每条 suggestion：先校验 new_line + improved_code 对齐
        inline_posted: list[str] = []
        inline_skipped: list[dict[str, Any]] = []
        for raw in suggestions:
            try:
                normalised = self._normalise_suggestion(raw)
            except ValueError as e:
                logger.warning(
                    "improve.skip_invalid_suggestion project={} mr={} sugg={} err={}",
                    self.project_id, self.mr_iid, raw, e,
                )
                inline_skipped.append({"suggestion": raw, "reason": str(e)})
                continue

            file_path = normalised["file"]
            start_line = normalised["new_line"]
            improved = normalised["improved_code"]
            existing = (raw.get("existing_code") or "").strip("\n") if isinstance(raw, dict) else ""
            decision = self._validate_suggestion(
                file_path=file_path,
                start_line=start_line,
                improved_code=improved,
                existing_code=existing,
                line_map=line_map,
                file_sources=file_sources,
            )
            # === A. 跨次去重 — 守卫所有发布动作 (post / general).
            # Why: 此前 dedup 只在 action == "post" 分支内, 收缩建议
            #      (action == "general") 走另一条路径直接发评论, 同一行
            #      在第二轮 improve 时会重复发 [issue: MR155 line 12 x 2].
            #      把 dedup 提前到 _validate_suggestion 之后, 让所有
            #      会发到 GitLab 的分支都先查重.
            if decision["action"] in ("post", "general"):
                _sev = (normalised.get("severity") or "medium").lower()
                _head_sha = self._get_mr_head_sha() or ""
                try:
                    from reviewagent.telemetry.store import get_store as _dedup_store
                    _dedup_db = _dedup_store()
                    if _dedup_db.suggestion_exists_at_line(
                        self.project_id, self.mr_iid, file_path,
                        decision["new_line"], _sev, head_sha=_head_sha,
                        line_tolerance=2,
                    ):
                        logger.info(
                            "improve.skip_at_line project={} mr={} file={} line={} severity={} head_sha={}",
                            self.project_id, self.mr_iid, file_path,
                            decision["new_line"], _sev,
                            _head_sha[:8] if _head_sha else "",
                        )
                        inline_skipped.append({"suggestion": raw, "reason": "duplicate_at_line"})
                        continue
                    import hashlib as _dedup_hl
                    _dedup_existing = (raw.get("existing_code") or "").strip("\n") if isinstance(raw, dict) else ""
                    _dedup_fingerprint = _dedup_hl.sha256(
                        _dedup_existing.strip().encode("utf-8")
                    ).hexdigest()[:24]
                    if _dedup_db.suggestion_exists_by_fingerprint(
                        self.project_id, self.mr_iid, _dedup_fingerprint
                    ):
                        logger.info(
                            "improve.skip_duplicate project={} mr={} file={} line={} fingerprint={}",
                            self.project_id, self.mr_iid, file_path,
                            decision["new_line"], _dedup_fingerprint,
                        )
                        inline_skipped.append({"suggestion": raw, "reason": "duplicate_fingerprint"})
                        continue
                except Exception as _e:
                    logger.warning("improve.dedup_check failed (non-fatal): {}", _e)

            if decision["action"] == "post":
                body_to_post = normalised["body"]
                nc = decision.get("normalised_code") or normalised["improved_code"]
                n_lines = len(nc.split("\n"))
                if nc != normalised["improved_code"]:
                    logger.info(
                        "improve.fix_indent project={} mr={} file={} line={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                    )
                    sev = normalised.get("severity", "medium").upper()
                    # suggestion:-0+N: +N = N lines AFTER comment line
                    # 替换 N+1 行 (注释行 + N 行后续行)
                    # 要替换 len(existing_lines) 行 → N = len - 1
                    existing = (raw.get("existing_code") or "").strip("\n") if isinstance(raw, dict) else ""
                    existing_lines = existing.split("\n") if existing else []
                    n_replace = max(0, len(existing_lines) - 1)
                    body_to_post = (
                        f"**[{sev}]** **{normalised['header']}** — {normalised['label']}\n\n"
                        f"{normalised['rationale']}\n\n"
                        f"```suggestion:-0+{n_replace}\n{nc}\n```"
                        + self.HELP_TEXT_FOOTER
                    )

                note_id = self.gitlab.post_mr_discussion(
                    self.project_id,
                    self.mr_iid,
                    body_to_post,
                    file_path=file_path,
                    new_line=decision["new_line"],
                )
                if note_id:
                    inline_posted.append(note_id)
                    logger.info(
                        "improve.post_inline project={} mr={} file={} line={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                    )
                    # 记录 suggestion 到 telemetry (用于后续 /adopt 验证 + 跨次去重)
                    try:
                        from reviewagent.telemetry.store import get_store
                        head_sha = self._get_mr_head_sha() or ""
                        existing = (raw.get("existing_code") or "").strip("\n") if isinstance(raw, dict) else ""
                        # === 跨次去重: file+line+existing_code 的指纹 ===
                        import hashlib as _hl
                        # LLM 不输出 rule_keys 字段 → 从 rationale 文本里抽 R-XXX / R-OTHER / SSD-RULE-* 前缀
                        _rationale = (normalised.get("rationale") or "") if isinstance(normalised, dict) else ""
                        _rk = re.findall(r"(?:^|[^A-Z0-9-])(R-OTHER-IMPACT:[a-z0-9_]+|R-OTHER:[a-z0-9_]+|R-[A-Z]+(?:-[A-Z0-9_]+)*|SSD-RULE-[A-Z0-9-]+)", _rationale)
                        _rk = list(dict.fromkeys(_rk))  # 去重保序
                        rule_keys = (raw.get("rule_keys") if isinstance(raw, dict) else None) or _rk
                        fingerprint = _hl.sha256(
                            (existing or "").strip().encode("utf-8")
                        ).hexdigest()[:24]
                        cohort_key = _hl.sha256(
                            f"{file_path}:{decision['new_line']}:{','.join(rule_keys)}".encode("utf-8")
                        ).hexdigest()[:24]
                        get_store().record_suggestion(
                            project_id=self.project_id,
                            mr_iid=self.mr_iid,
                            note_id=note_id,
                            file_path=file_path,
                            target_line=decision["new_line"],
                            target_line_end=(decision["new_line"] + n_lines - 1) if n_lines > 1 else decision["new_line"],
                            existing_code=existing,
                            improved_code=nc,
                            header=normalised.get("header"),
                            severity=normalised.get("severity"),
                            head_sha=head_sha,
                            label=normalised.get("label"),
                            rule_keys=rule_keys,
                            one_sentence_summary=(raw.get("one_sentence_summary") or normalised.get("rationale")) if isinstance(raw, dict) else None,
                            importance=raw.get("importance") if isinstance(raw, dict) else None,
                            fingerprint=fingerprint,
                            cohort_key=cohort_key,
                        )
                    except Exception as e:
                        logger.warning("improve.record_suggestion failed: {}", e)
                else:
                    inline_skipped.append({"suggestion": raw, "reason": "gitlab_rejected"})
            elif decision["action"] == "general":
                # 收缩建议: 发普通评论（无 Apply 按钮），让用户看到建议但不能一键删除代码
                sev = normalised.get("severity", "medium").upper()
                general_body = (
                    f"**[{sev}]** **{normalised['header']}** — {normalised['label']}\n\n"
                    f"{normalised['rationale']}\n\n"
                    f"> ⚠️ 该建议涉及代码收缩（删除行），请人工评估后手动修改"
                    + self.HELP_TEXT_FOOTER
                )
                note_id = self.gitlab.post_mr_discussion(
                    self.project_id,
                    self.mr_iid,
                    general_body,
                    file_path=file_path,
                    new_line=decision["new_line"],
                )
                if note_id:
                    inline_posted.append(note_id)
                    logger.info(
                        "improve.post_general project={} mr={} file={} line={} reason={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                        decision["reason"],
                    )
                else:
                    inline_skipped.append({"suggestion": raw, "reason": "gitlab_rejected"})
            else:
                # action == "drop" — 不发任何评论, 仅记 telemetry
                inline_skipped.append({"suggestion": raw, "reason": decision["reason"]})

        return {
            "top_comment_id": top_comment_id,
            "suggestions_count": len(suggestions),
            "inline_posted": len(inline_posted),
            "inline_skipped": len(inline_skipped),
        }

    def _validate_suggestion(
        self,
        *,
        file_path: str,
        start_line: int,
        improved_code: str,
        existing_code: str = "",
        line_map: dict[str, set[int]],
        file_sources: dict[str, list[str]],
    ) -> dict[str, Any]:
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
            file_sources[file_path] = self._read_file_lines(file_path)
        file_lines = file_sources[file_path]

        # 2. 用 existing_code 反查真实行号
        actual_line: int | None = None
        if existing_code and existing_code.strip():
            actual_line = self._find_line_by_existing_code(
                file_lines, existing_code, hint_line=start_line, max_window=8,
            )
            if actual_line is not None and actual_line != start_line:
                logger.info(
                    "improve.snap_to_existing project={} mr={} file={} {} -> {} (from existing_code)",
                    self.project_id, self.mr_iid, file_path, start_line, actual_line,
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
                    self.project_id, self.mr_iid, file_path, start_line,
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
                    self.project_id, self.mr_iid, file_path, start_line, snapped,
                )
                start_line = snapped

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
                    t.strip() == a.strip() for t, a in zip(tail_lines, after_lines)
                ):
                    improved_lines = improved_lines[:-n_added]
                    improved_code = "\n".join(improved_lines)
                    logger.info(
                        "improve.trim_trailing_dup project={} mr={} file={} "
                        "line={} trimmed={}",
                        self.project_id, self.mr_iid, file_path,
                        start_line, n_added,
                    )

            normalised_code = self._fix_indent(target_line_raw, improved_code)
            logger.info(
                "improve.multiline_replace project={} mr={} file={} line={} existing_lines={} improved_lines={}",
                self.project_id, self.mr_iid, file_path, start_line,
                len(existing_lines), len(improved_lines),
            )
            # 更新 normalised_code 到 normalisation 结果 (在 publish 阶段生效)
            return {"action": "post", "new_line": start_line, "reason": "multi_line_replacement",
                    "normalised_code": normalised_code}

        # 4b. 对齐检查
        # N→N 等行数替换: existing_code 已在文件中定位 (actual_line 非 None),
        #   agent 有意改写代码 (e.g. print→logger.info), 第一行不需要匹配.
        # 1→1 且 existing_code 未找到: 仍需校验第一行对齐, 防止模型乱发.
        is_same_line_count = bool(existing_lines) and len(improved_lines) == len(existing_lines)
        if not (is_same_line_count and actual_line is not None):
            if not _code_first_line_matches(target_line, imp_first):
                return {"action": "drop", "new_line": start_line,
                        "reason": f"improved_code first line doesn't match file:{start_line} ({target_line!r} vs {imp_first!r})"}

        # 4c. 收缩检查: M < N 时一律降级为普通评论（无 Apply 按钮）
        #     收缩建议移除代码的风险太高 — agent 经常把不该删的行包进 existing_code
        #     导致 Apply 后丢失关键逻辑。降级后用户仍能看到建议文本，但不能一键应用。
        if len(improved_lines) < len(existing_lines):
            logger.info(
                "improve.shrink_to_general project={} mr={} file={} "
                "line={} existing={} improved={}",
                self.project_id, self.mr_iid, file_path,
                start_line, len(existing_lines), len(improved_lines),
            )
            return {"action": "general", "new_line": start_line,
                    "reason": f"shrinking suggestion ({len(existing_lines)} -> {len(improved_lines)} lines)"}

        # 4d. 缩进修正: 若 improved_code 第一行缺缩进, 自动补齐
        normalised_code = self._fix_indent(target_line_raw, improved_code)

        return {"action": "post", "new_line": start_line, "reason": "ok",
                "normalised_code": normalised_code}

    @staticmethod
    def _score_suggestion(s: dict) -> int:
        """对单条建议评分 (0~100), 用于过滤低质量建议."""
        score = 0
        # severity (0~35)
        sev_map = {"critical": 35, "high": 30, "medium": 20, "low": 10}
        score += sev_map.get((s.get("severity") or "medium").lower(), 20)
        # label (0~30) — cross-file impact 与 potential bug 同级, 体现 P1 优先级
        label_map = {
            "security": 30, "potential bug": 25, "cross-file impact": 25, "performance": 20,
            "enhancement": 15, "code quality": 12, "style": 5,
        }
        score += label_map.get((s.get("label") or "").lower(), 10)
        # rationale 长度 (0~20)
        rationale = (s.get("rationale") or "")
        if len(rationale) > 200:
            score += 20
        elif len(rationale) > 100:
            score += 15
        elif len(rationale) > 50:
            score += 8
        # 规则引用 (+10)
        if _RULE_REF_REGEX.search(rationale):
            score += 10
        # header 质量 (+5)
        header = (s.get("header") or "").strip()
        if len(header) >= 4 and header != "建议改进":
            score += 5
        return min(100, score)

    @staticmethod
    def _fix_indent(target_line: str, improved_code: str) -> str:
        """确保 improved_code 第一行的缩进 == target_line 的缩进.

        模型有时会忘记给 improved 第一行加缩进, 导致 Apply 后格式错乱.
        例如: target=`    q = f"..."`, improved=`q = "..."\n    return ...`
        → 修正为 `    q = "..."\n    return ...`
        """
        lines = improved_code.split("\n")
        if not lines or not target_line:
            return improved_code
        # 提取 target 的前导空白
        target_indent = target_line[: len(target_line) - len(target_line.lstrip())]
        # 第一行当前前导空白
        first = lines[0]
        first_indent_len = len(first) - len(first.lstrip())
        target_indent_len = len(target_indent)
        if first_indent_len < target_indent_len:
            # 补齐缺失的缩进
            pad = target_indent[first_indent_len:]
            lines[0] = pad + first
            return "\n".join(lines)
        return improved_code

    @staticmethod
    def _normalise_suggestion(s: dict[str, Any]) -> dict[str, Any]:
        """校验 + 构造 GitLab "Apply suggestion" inline comment body."""
        if not isinstance(s, dict):
            raise ValueError(f"suggestion must be dict, got {type(s).__name__}")
        file_path = s.get("file")
        if not file_path or not isinstance(file_path, str):
            raise ValueError("missing 'file' (str)")
        start_line = s.get("start_line")
        if not isinstance(start_line, int) or start_line <= 0:
            raise ValueError("missing 'start_line' (int > 0)")
        existing = (s.get("existing_code") or "").strip("\n")
        improved = (s.get("improved_code") or "").strip("\n")
        if not improved:
            raise ValueError("missing 'improved_code' (non-empty)")

        header = (s.get("header") or "建议改进").strip()
        rationale = (s.get("rationale") or "").strip()
        label = (s.get("label") or "enhancement").strip()
        severity = (s.get("severity") or "medium").strip().lower()

        # GitLab suggestion 格式: ```suggestion:-A+B
        # - A = 注释行往上删除的行数 (0 = 不删上方行)
        # - B = 注释行往下 (含注释行自身) 替换的行数
        #   GitLab 语义: +B = B lines AFTER the commented line
        #   总替换 = A + 1 (注释行) + B = A + B + 1
        #   要替换 N 行 existing → B = N - 1
        # 例: 替换 line 59~62 (4行) → suggestion:-0+3
        # 注意: -M 会删除 new_line 上方的 M 行，绝对不能用 existing 行数做 M!
        existing_lines = existing.split("\n") if existing else []
        n_replace = max(0, len(existing_lines) - 1)

        body = (
            f"**[{severity.upper()}]** **{header}** — {label}\n\n"
            f"{rationale}\n\n"
            f"```suggestion:-0+{n_replace}\n{improved}\n```"
            + ImproveCommand.HELP_TEXT_FOOTER
        )
        return {
            "file": file_path,
            "new_line": start_line,
            "improved_code": improved,
            "header": header,
            "rationale": rationale,
            "label": label,
            "severity": severity,
            "body": body,
        }


def _code_first_line_matches(target_line: str, improved_first: str) -> bool:
    """启发式: target / improved 第一行是否同一行（容忍空格/标点差异）.

    规则（任一命中即视为同一行）:
      1. 字符串相等（strip 后）
      2. 都是 def 行且函数名一致
      3. 都是 class 行且类名一致
      4. 都是赋值行且左侧变量名一致（`q = ...` vs `q = ...`）
      5. 都是 return 行且第一个 token 一致
      6. 共享 ≥3 字符标识符（去掉通用 stop token）
    """
    import re

    if not target_line or not improved_first:
        return False
    if target_line == improved_first:
        return True

    # 0. 前缀必须一致（去除前导空白后前 4 字符一致）— 防止 "sys.stderr.write" vs "def log_event"
    # 这种整行重写的情况
    # 注：调用方应在调用前已经判定 "这是多行替换" (improved 行数 > existing 行数),
    # 在那种情况下 improved_first 不需要和 target_line 是同类操作 (e.g. return → with),
    # 所以多行替换应当从外部直接放行, 不进此函数
    t_pref = target_line.lstrip()[:4]
    f_pref = improved_first.lstrip()[:4]
    if t_pref != f_pref:
        return False

    def _lstrip(s: str) -> str:
        return s.lstrip()

    t = _lstrip(target_line)
    f = _lstrip(improved_first)

    # 同 def 行（函数名一致）
    if t.startswith("def ") and f.startswith("def "):
        m_t = re.match(r"def\s+(\w+)", t)
        m_f = re.match(r"def\s+(\w+)", f)
        if m_t and m_f and m_t.group(1) == m_f.group(1):
            return True

    # 同 class 行
    if t.startswith("class ") and f.startswith("class "):
        m_t = re.match(r"class\s+(\w+)", t)
        m_f = re.match(r"class\s+(\w+)", f)
        if m_t and m_f and m_t.group(1) == m_f.group(1):
            return True

    # except 行（bare except → typed except）— 两者都以 except: 开头
    if t.startswith("except") and f.startswith("except"):
        # 都是 except 关键字开头, 即便 f 加了 (X, Y) 参数也算同一行
        return True

    # 赋值 / 调用同名前缀
    m_t = re.match(r"([A-Za-z_]\w*)\s*[=(.]", t)
    m_f = re.match(r"([A-Za-z_]\w*)\s*[=(.]", f)
    if m_t and m_f and m_t.group(1) == m_f.group(1):
        return True

    # return 行（共享 ≥5 字符非 stop 标识符 — 同一行 return ... 但内部计算表达式不同）
    # 停用词只过滤关键字，不过滤数据键名（price/items 是判断"同一行"的关键信号）
    if t.startswith("return ") and f.startswith("return "):
        _stop = {"def", "return", "self", "for", "while", "in", "is", "and", "or", "not",
                 "if", "else", "elif", "import", "from", "class", "try", "except",
                 "finally", "with", "as", "raise", "pass", "lambda", "yield"}
        t_toks = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b", t)) - _stop
        f_toks = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b", f)) - _stop
        return bool(t_toks & f_toks)

    # token overlap（≥3 字符，去 stop — 包括关键字 + Python 内置函数）
    stop = {"def", "return", "self", "None", "True", "False", "and", "or",
            "not", "if", "else", "elif", "for", "while", "in", "is",
            "import", "from", "class", "try", "except", "finally",
            "with", "as", "raise", "pass", "lambda", "yield",
            # Python builtins: 不同行共用它们不等于 "同一行"
            "open", "close", "read", "write", "print", "len", "str", "int",
            "list", "dict", "set", "tuple", "sum", "min", "max", "abs",
            "range", "type", "isinstance", "getattr", "setattr", "delattr",
            "hasattr", "callable", "repr", "format", "hash", "id", "iter",
            "next", "map", "filter", "zip", "enumerate", "sorted", "reversed",
            "any", "all", "bool", "bytes", "bytearray", "complex", "float",
            "frozenset", "object", "property", "staticmethod", "classmethod",
            "super", "input", "eval", "exec", "compile", "globals", "locals",
            "vars", "dir", "chr", "ord", "hex", "oct", "bin", "round",
            "divmod", "pow", "slice", "memoryview", "ascii", "breakpoint",
            "__init__", "__name__", "__main__", "__file__", "__doc__",
            "json", "csv", "sys", "os", "sqlite3",
            # 跨 `os.environ.get(...)` 等通用调用模板
            "environ", "getenv", "get", "set", "items", "keys", "values",
            "append", "extend", "update", "insert", "remove", "delete",
            "split", "join", "strip", "lower", "upper", "replace",
            "encode", "decode", "startswith", "endswith",
            "request", "response", "method", "args", "kwargs",
            # 通用 dict key / 属性名 — 不同对象共用纯属巧合
            "name", "value", "price", "count", "total", "size", "length",
            "type", "status", "level", "code", "message", "msg",
            "data", "result", "error", "info", "warning", "debug",
            "created", "updated", "deleted", "modified", "timestamp",
            "id", "uuid", "key", "secret", "password", "username",
            "host", "port", "addr", "address", "ip", "domain",
            # 常见短变量名 — 跨行共用纯属巧合
            "path", "file", "data", "name", "key", "val", "obj", "res",
            "tmp", "new", "old", "src", "dst", "err", "out", "msg", "cfg",
            "ctx", "args", "kwargs", "item", "rows", "row", "conn", "cur",
            "fp", "fd", "buf", "tag", "ids", "env", "opts", "url", "uri",
            "ret", "result", "status", "count", "total",
            "token", "email", "user"}
    t_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", t))
    f_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", f))
    overlap = (t_tokens & f_tokens) - stop
    if overlap:
        # 至少有一个 shared token 长度 ≥ 5（排除短缩写）
        return any(len(tok) >= 5 for tok in overlap)

    return False
