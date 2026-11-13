"""检查部署态 agent 提示词是否与当前 profile prompts 一致（防静默漂移）。

背景:
`profiles/<profile>/prompts/improve.md` 是 **system prompt 的唯一来源** ——
两个 provider 都是**每次调用现读 profile**（`qoder_sdk_provider.py` 把
`meta["prompt"]` 喂给 SDK；`qodercli_subprocess.py` 用 `--append-system-prompt`
拼命令行）。因此改 profile 里的 prompt 并重启服务，system prompt 立刻生效。

`.qoder/agents/improve.md` 是**另一份物化**：QoderCLI ACP server 启动时读取的
**子代理注册表**（name / description / tools / permissionMode / maxTurns / model），
正文是同一份 prompt。派生出来的子代理跑的是这份正文。

`sync_qoder_agents.py` 按 **mtime** 增量写，漏跑一次（或 mtime 判断失误）就会让
注册表与 profile 不一致 —— ACP 侧派生出的子代理仍按旧约束跑，而且**没有任何
静态检查会报**。

本脚本按**内容**比对（不看 mtime），覆盖两种漂移:
* 缺失: `prompts/<stem>.md` 有对应 prompt，但 `agents/<stem>.md` 不存在；
* 内容漂移: 部署态文件与按当前 prompt 渲染的正文不一致。

`agents/` 下的多余文件只做提示（warning），不算失败 —— 例如历史遗留的
`improve_agent.md`（旧 `reviewagent/prompts/` 布局的产物，当前无 prompt 源）。

用法:
  set -a; source config/instance.env; set +a
  .venv/bin/python scripts/ops/check_agent_drift.py            # 检查当前激活 profile
  .venv/bin/python scripts/ops/check_agent_drift.py --profile ssd-python-autotest
退出码: 0 = 无漂移；1 = 存在缺失/内容漂移（stdout 逐条列出）。

注意: `reviewagent.config` 在 import 期就要求 `GITLAB_*` 必填项，必须先
source 实例配置再运行本脚本。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.sync_qoder_agents import expected_agent_text  # noqa: E402


def check_drift(prompts_dir: Path, agents_dir: Path) -> tuple[list[str], list[str]]:
    """比对 prompts_dir 与 agents_dir.

    Returns:
        (errors, warnings) —— errors 为缺失/内容漂移，warnings 为多余文件。
    """
    errors: list[str] = []
    warnings: list[str] = []
    expected_stems: set[str] = set()

    for src in sorted(prompts_dir.glob("*.md")):
        if src.stem.startswith("_"):
            continue
        expected_stems.add(src.stem)
        dst = agents_dir / f"{src.stem}.md"
        if not dst.exists():
            errors.append(f"缺失: {dst}（源 {src.name} 未物化）")
            continue
        want = expected_agent_text(src)
        try:
            got = dst.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 读失败按漂移处理
            errors.append(f"读取失败: {dst} ({exc})")
            continue
        if got != want:
            errors.append(
                f"内容漂移: {dst}（源 {src.name} 已更新，部署态未同步；"
                f"期望 {len(want)} 字节，实际 {len(got)} 字节）"
            )

    if agents_dir.is_dir():
        for dst in sorted(agents_dir.glob("*.md")):
            if dst.stem not in expected_stems:
                warnings.append(f"多余: {dst}（无对应 prompt 源，可能是历史遗留）")

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="检查部署态 agent 提示词漂移")
    parser.add_argument("--profile", default=None, help="profile 名（默认取激活 profile）")
    parser.add_argument("--agents-dir", default=None, help="部署态 agent 目录（默认 <repo>/.qoder/agents）")
    args = parser.parse_args()

    from reviewagent.scenario.profile import active_or_load, load_profile

    profile = load_profile(args.profile) if args.profile else active_or_load()
    prompts_dir = profile.root / "prompts"
    agents_dir = Path(args.agents_dir) if args.agents_dir else REPO / ".qoder" / "agents"

    errors, warnings = check_drift(prompts_dir, agents_dir)
    for line in warnings:
        print(f"WARN  {line}")
    for line in errors:
        print(f"ERROR {line}")

    if errors:
        print(f"\nagent 提示词漂移 {len(errors)} 项 —— 运行 scripts/sync_qoder_agents.py 后重启服务")
        return 1
    print(f"agent 提示词一致: {prompts_dir} -> {agents_dir}（{len(warnings)} warning）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
