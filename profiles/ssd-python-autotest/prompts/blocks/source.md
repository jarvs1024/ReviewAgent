<!-- @section:full -->
### 完整源码: `{{file_path}}` (共 {{total}} 行)
```
{{numbered}}
```
<!-- @section:full_empty -->
### 完整源码
(无法读取 {{file_path}})
<!-- @section:full_truncated -->
### 完整源码: `{{file_path}}` (共 {{total}} 行, **已截断到前 {{limit}} 行**)
⚠️ 末尾 {{omitted}} 行未加载, 如需检视请缩小 diff 范围或拆分 MR
```
{{numbered}}
```
<!-- @section:partial -->
### diff 上下文 (partial, ±{{n}} 行): `{{file_path}}`
⚠️ 此文件超出 IMPROVE_FULL_FILES={{full_files}} 配额, 已降级为 partial 模式
⚠️ 仅显示 diff 附近 {{segments}} 段 (±{{n}} 行), 跨段语义识别能力下降
```
{{blocks}}
```
<!-- @section:partial_empty -->
### 上下文 (partial)
(无法读取 {{file_path}})
<!-- @section:patch -->
### patch-only: `{{file_path}}`
⚠️ 已降级为 patch-only (overflow 或低优先级), 仅 diff + ±{{n}} 行最小 context
⚠️ 跨行/跨段语义识别能力有限, 优先识别行内问题 (裸 except / 可变默认参数 / 硬编码等)
建议关注行: {{valid_display}}{{valid_suffix}}
{{context_block}}
<!-- @section:patch_context -->
### 最小上下文 (patch, ±{{n}} 行, 前 {{shown}} 段)
```
{{blocks}}
```
<!-- @section:patch_context_omitted -->
### 最小上下文 (patch, ±{{n}} 行, 前 {{shown}} 段)
```
{{blocks}}
```
(另有 {{omitted}} 段未展示)
