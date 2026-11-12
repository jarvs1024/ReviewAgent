<!-- @section:chunk -->
{{cross_file_section}}{{rules_block}}{{prior_section}}## 本次检视文件: `{{file_path}}`

### diff
```diff
{{file_diff}}
```

{{source_block}}

### VALID NEW LINES（start_line 只能从此取）

{{valid_lines}}

## 输出

按 system prompt 输出 JSON。**summary_md 输出空字符串 `""`**，只输出本文件的 suggestions。
<!-- @section:batch -->
{{rules_block}}## 本次检视共 {{file_count}} 个文件 (小改动合并批次)

**每条 suggestion 的 `file` 字段只能从以下路径中选:**
{{file_list}}

---

{{sections}}

## 输出

按 system prompt 输出 JSON。**summary_md 输出空字符串 `""`**。
输出本批次全部文件的 suggestions 合并列表; 每条必须带正确的 `file` 字段 (file 不在上述清单中的 suggestion 会被丢弃)。
<!-- @section:rules_general -->
{{general_rules}}

<!-- @section:rules_repo -->
## 🟠 优先 2 — 仓库自定义规则（运行时从被检仓库获取）

规则来源是本次注入的 `<rule_index>`（键清单）与 `<instruction_files>`（被检仓库配置的规则目录下规则文件原文）。
先逐条扫描，命中即产 suggestion，`rationale` 引用规则键。

**获取协议（强制）**：
- 目录、前缀、规则条数、文件顺序全部以注入内容为准；不要假设固定路径 / 固定前缀 / 固定条数。
- 只允许引用 `<rule_index>` 或 `<instruction_files>` 中**字面出现**的规则键；原样复制，不翻译、不改写、不拼接、不臆造。
- 每条 suggestion 的 `rationale` 按对应规则文件的「审查者引用模板」填写（规则键 + `文件:行号` + 模板要求的字段）。
- 规则文件声明的必填标记 / 必填章节按原文照抄。
- 注入内容里没有的键一律视为幻觉，禁止输出。

{{repo_rules}}

---

{{general_rules}}

