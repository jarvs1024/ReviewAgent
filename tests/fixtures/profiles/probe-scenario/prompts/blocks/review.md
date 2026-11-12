<!-- @section:chunk -->
{{cross_file_section}}{{rules_block}}{{prior_section}}## 探针检视文件（probe-scenario）: `{{file_path}}`

### diff
```diff
{{file_diff}}
```

{{source_block}}

### VALID NEW LINES（仅这些行可以挂 suggestion）

{{valid_lines}}

## 输出

只输出本文件的 suggestions（summary_md 为空字符串）。
<!-- @section:batch -->
{{rules_block}}## 探针批次：共 {{file_count}} 个文件

以下路径是唯一合法的 `file` 字段取值：

{{file_list}}

---

{{sections}}

## 输出

合并输出本批次全部文件的 suggestions（每条必须带 `file`）。
<!-- @section:rules_general -->
{{general_rules}}
<!-- @section:rules_repo -->
## 探针仓库规则（优先）

{{repo_rules}}

---

{{general_rules}}
