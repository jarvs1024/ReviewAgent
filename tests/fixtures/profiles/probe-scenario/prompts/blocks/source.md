<!-- @section:full -->
### 探针完整源码: `{{file_path}}` (共 {{total}} 行)
```
{{numbered}}
```
<!-- @section:full_empty -->
### 探针完整源码
(探针无法读取 {{file_path}})
<!-- @section:full_truncated -->
### 探针完整源码: `{{file_path}}` (共 {{total}} 行, **探针截断到前 {{limit}} 行**)
⚠️ 探针未加载末尾 {{omitted}} 行
```
{{numbered}}
```
<!-- @section:partial -->
### 探针 diff 上下文 (partial, ±{{n}} 行): `{{file_path}}`
⚠️ 探针文件超出 full_files={{full_files}} 配额, 已降级为 partial 模式
⚠️ 探针仅显示 diff 附近 {{segments}} 段 (±{{n}} 行)
```
{{blocks}}
```
<!-- @section:partial_empty -->
### 探针上下文 (partial)
(探针无法读取 {{file_path}})
<!-- @section:patch -->
### 探针 patch-only: `{{file_path}}`
⚠️ 探针已降级为 patch-only, 仅 diff + ±{{n}} 行最小 context
建议探针关注行: {{valid_display}}{{valid_suffix}}
{{context_block}}
<!-- @section:patch_context -->
### 探针最小上下文 (patch, ±{{n}} 行, 前 {{shown}} 段)
```
{{blocks}}
```
<!-- @section:patch_context_omitted -->
### 探针最小上下文 (patch, ±{{n}} 行, 前 {{shown}} 段)
```
{{blocks}}
```
(探针另有 {{omitted}} 段未展示)
