"""回填 mr_activity 表中缺失的时间字段.

从 GitLab API 拉取 MR 真实数据, 用 COALESCE 模式更新:
- created_at: 仅当 DB 为 NULL 时填充
- updated_at: 仅当 DB 为 NULL 时填充
- merged_at: 仅当 DB 为 NULL 时填充
- state: 始终更新为最新值

用法:
    python3 scripts/backfill_mr_times.py [--project-id ID] [--dry-run]
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone

import gitlab

# 从 .env 读取配置 (与 reviewagent 一致)
def load_env():
    env = {}
    try:
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def fmt_dt(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int, default=0, help="0=all projects")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    env = load_env()
    gitlab_url = env.get("GITLAB_URL", "http://10.20.27.7")
    gitlab_token = env.get("GITLAB_PERSONAL_ACCESS_TOKEN", "")
    db_path = env.get("REVIEWAGENT_DATA_DIR", "./data") + "/telemetry.db"

    if not gitlab_token:
        print("ERROR: GITLAB_PERSONAL_ACCESS_TOKEN not found in .env")
        sys.exit(1)

    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 找所有时间字段有 NULL 的 MR
    sql = """
        SELECT project_id, mr_iid, state, created_at, updated_at, merged_at
        FROM mr_activity
        WHERE created_at IS NULL OR updated_at IS NULL OR merged_at IS NULL
    """
    params = []
    if args.project_id:
        sql += " AND project_id = ?"
        params.append(args.project_id)

    rows = conn.execute(sql, params).fetchall()
    print(f"Found {len(rows)} MRs with NULL time fields")

    updated = 0
    for row in rows:
        pid = row["project_id"]
        iid = row["mr_iid"]
        needs = []
        if row["created_at"] is None: needs.append("created_at")
        if row["updated_at"] is None: needs.append("updated_at")
        if row["merged_at"] is None: needs.append("merged_at")

        # 从 GitLab API 拉取 MR 真实数据
        try:
            mr = gl.projects.get(pid).mergerequests.get(iid)
        except Exception as e:
            print(f"  !{iid} (project={pid}): API error: {e}")
            continue

        api_created = parse_dt(mr.created_at)
        api_updated = parse_dt(mr.updated_at)
        api_merged = parse_dt(mr.merged_at)
        api_state = mr.state

        sets = []
        vals = []

        # created_at: COALESCE 模式
        if row["created_at"] is None and api_created:
            sets.append("created_at = ?")
            vals.append(fmt_dt(api_created))

        # updated_at: 用 API 最新值 (updated_at 应该总是更新)
        if row["updated_at"] is None and api_updated:
            sets.append("updated_at = ?")
            vals.append(fmt_dt(api_updated))

        # merged_at: COALESCE 模式
        if row["merged_at"] is None and api_merged:
            sets.append("merged_at = ?")
            vals.append(fmt_dt(api_merged))

        # state: 始终同步
        if row["state"] != api_state:
            sets.append("state = ?")
            vals.append(api_state)

        if not sets:
            print(f"  !{iid} (project={pid}): nothing to update (API also NULL)")
            continue

        vals.extend([pid, iid])
        update_sql = f"UPDATE mr_activity SET {', '.join(sets)} WHERE project_id = ? AND mr_iid = ?"

        print(f"  !{iid} (project={pid}): {', '.join(sets)}")
        if not args.dry_run:
            conn.execute(update_sql, vals)
            conn.commit()
        updated += 1

    conn.close()
    mode = " (DRY RUN)" if args.dry_run else ""
    print(f"\nUpdated {updated}/{len(rows)} MRs{mode}")


if __name__ == "__main__":
    main()
