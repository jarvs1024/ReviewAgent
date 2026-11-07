{{rules_block}}## 本次检视共 {{file_count}} 个文件 (小改动合并批次)

**每条 suggestion 的 `file` 字段只能从以下路径中选:**
{{file_list}}

---

{{sections}}

## 输出

按 system prompt 输出 JSON。**summary_md 输出空字符串 `""`**。
输出本批次全部文件的 suggestions 合并列表; 每条必须带正确的 `file` 字段 (file 不在上述清单中的 suggestion 会被丢弃)。
