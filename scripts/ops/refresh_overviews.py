"""重刷「检视汇总」note —— 修 cohort_key 口径迁移后历史 MR 的汇总数字滞后。

背景:
`reviewagent/telemetry/store.py` 的 cohort_key 口径迁移（`PRAGMA user_version=1`）
把旧算法（file+header）换成新算法（file+header+内容指纹）。旧算法会把
「同文件、同标题、不同位置」的独立问题误并成一个 cohort → 汇总少算、采纳率虚高。

迁移**只回填 DB 的 cohort_key**，不会回头刷新已经发布在 GitLab 上的
「## 检视汇总」note —— 于是历史 MR 会出现：
* 图2（inline 评论，1:1 逐条）= 真实条数（例如 30）
* 图1（顶部汇总 note）= 迁移前的旧口径（例如 23）
→ 用户看到的总数比实际评论数少。

实测锚点: p31 MR !4 —— 迁移前 L3 20/20，迁移后图1 四项对不上（16/20）。

本脚本做「只读判定 + 可选重刷」:
1. 扫 DB 找出有 suggestion 的 (project_id, mr_iid)；
2. 用**当前** cohort 口径算出应显示的总数，与 GitLab 上已发布的汇总 note 对比；
3. 不一致时按需重刷（`publish_overview` 是 find/update 型，幂等）。

安全约束:
* 默认 `--dry-run`：只报告哪些 MR 的汇总数字对不上；
* 只**更新**已有的汇总 note（`publish_overview` 找不到才新建），不删任何内容；
* 不触发任何检视（不调 LLM），只重算 + 重发汇总正文。

用法:
  set -a; source config/instance.env; set +a
  .venv/bin/python scripts/ops/refresh_overviews.py                 # 全量 dry-run
  .venv/bin/python scripts/ops/refresh_overviews.py --project 31    # 指定项目 dry-run
  .venv/bin/python scripts/ops/refresh_overviews.py --project 31 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from reviewagent.config import current  # noqa: E402
from reviewagent.telemetry.store import get_store  # noqa: E402

_TOTAL_RE = re.compile(r"总建议数\s*(\d+)")
_OVERVIEW_HEADER = "## 检视汇总"


def _ensure_env() -> None:
    for k in ("GITLAB_URL", "GITLAB_PERSONAL_ACCESS_TOKEN"):
        if not os.environ.get(k):
            raise RuntimeError(
                f"missing required env var: {k}; source config/instance.env first"
            )


def _mr_pairs(project_id: int | None) -> list[tuple[int, int]]:
    store = get_store()
    with store._conn() as conn:
        if project_id is None:
            rows = conn.execute(
                "SELECT DISTINCT project_id, mr_iid FROM suggestions ORDER BY project_id, mr_iid"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT project_id, mr_iid FROM suggestions WHERE project_id=? "
                "ORDER BY mr_iid",
                (project_id,),
            ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _db_total(project_id: int, mr_iid: int) -> int:
    store = get_store()
    rows = [
        r for r in store.list_latest_by_cohort(project_id=project_id, mr_iid=mr_iid)
        if str(r.get("state")) != "superseded"
    ]
    return len(rows)


def _published_total(project_id: int, mr_iid: int) -> int | None:
    from reviewagent.gitlab.client import GitLabClient

    gl = GitLabClient()
    for n in gl.list_mr_notes(project_id, mr_iid):
        body = n.get("body") or ""
        if not body.startswith(_OVERVIEW_HEADER):
            continue
        m = _TOTAL_RE.search(body)
        if m:
            return int(m.group(1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="重刷检视汇总 note（cohort 口径迁移后）")
    ap.add_argument("--project", type=int, default=None, help="只处理指定 project_id")
    ap.add_argument("--apply", action="store_true", help="真的重刷（默认只报告）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    _ensure_env()
    current()  # 触发配置解析（fail-fast）
    pairs = _mr_pairs(args.project)
    report: list[dict] = []
    for pid, mr in pairs:
        try:
            db_total = _db_total(pid, mr)
            pub_total = _published_total(pid, mr)
        except Exception as e:  # noqa: BLE001
            report.append({"project_id": pid, "mr_iid": mr, "error": str(e)})
            continue
        item = {
            "project_id": pid, "mr_iid": mr,
            "db_total": db_total, "published_total": pub_total,
            "needs_refresh": pub_total is not None and pub_total != db_total,
        }
        if item["needs_refresh"] and args.apply:
            try:
                from reviewagent.commands._common import publish_overview

                note_id = publish_overview(
                    project_id=pid, mr_iid=mr,
                    inline_posted_count=0, run_late_detect=False,
                )
                item["refreshed_note_id"] = note_id
                item["applied"] = True
            except Exception as e:  # noqa: BLE001
                item["applied"] = False
                item["error"] = str(e)
        report.append(item)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        stale = [r for r in report if r.get("needs_refresh")]
        print(f"扫描 {len(report)} 个 MR，待重刷 {len(stale)} 个"
              f"{'（--apply 才执行）' if stale and not args.apply else ''}")
        for r in report:
            flag = "⟳" if r.get("needs_refresh") else "·"
            print(f"  {flag} project {r['project_id']} MR !{r['mr_iid']}: "
                  f"DB={r.get('db_total')} 已发布={r.get('published_total')}"
                  + (f" applied={r.get('applied')} note={r.get('refreshed_note_id')}"
                     if r.get("needs_refresh") else "")
                  + (f" error={r['error']}" if r.get("error") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
