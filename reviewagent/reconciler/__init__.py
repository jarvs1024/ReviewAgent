"""Periodic reconciler — 安全网, 处理"纯 click-only 无后续事件" 孤儿状态.

GitLab 17.5 偶尔不发 "marked this discussion as resolved" webhook 给
note_events hook. publish_overview pre-reconcile 已经覆盖了"用户 click 后
还会有其他事件 (push, /adopt, ...)" 的情况. 但"用户纯 click 之后啥都没做"
的场景下, 没有任何事件触发 publish_overview, DB 会永远停在 open.

本模块提供 reconcile_open_mrs() 每隔一段时间 (默认 60s) 扫所有 bot 跟踪的
open MR, 把 GitLab 已 resolved 但 DB 还 open 的 suggestion 标 resolved,
刷新对应 MR 的 检视汇总 note.

入口:
- scripts/run_reconciler.sh — 启动 daemon (建议用 launchd StartInterval=60 调起)
- reconcile_open_mrs() — 可直接 import 调用 (用于测试 / 一次性手动跑)
"""
