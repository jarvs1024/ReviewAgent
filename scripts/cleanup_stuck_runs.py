"""清理 stuck running jobs + 回填 MR 1087 缺失字段.

一次性运维脚本, 在 86 服务器上执行.
"""
import sqlite3
from datetime import datetime, timezone

DB_PATH = "/home/workflow/data/telemetry.db"


def fix_stuck_runs():
    """把所有 status='running' 的记录标记为 failed."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    stuck = conn.execute(
        "SELECT id, mr_iid, command, started_at FROM review_runs WHERE status = 'running'"
    ).fetchall()
    print(f"Found {len(stuck)} stuck 'running' jobs")

    now = datetime.now(timezone.utc).isoformat()
    fixed = 0
    for r in stuck:
        d = dict(r)
        started = d["started_at"] or ""
        # 计算 duration_ms
        try:
            start_dt = datetime.fromisoformat(started)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            dur_ms = int((datetime.now(timezone.utc) - start_dt).total_seconds() * 1000)
        except Exception:
            dur_ms = 0

        conn.execute(
            "UPDATE review_runs SET status = 'failed', "
            "error = 'stuck running: process terminated unexpectedly', "
            "finished_at = ?, duration_ms = ? WHERE id = ?",
            (now, dur_ms, d["id"]),
        )
        print(f"  fixed id={d['id']} mr=!{d['mr_iid']} cmd={d['command']} started={started}")
        fixed += 1

    conn.commit()
    conn.close()
    print(f"Fixed {fixed} stuck runs")


def backfill_mr_1087():
    """从 GitLab API 回填 MR 1087 的 updated_at / merged_at."""
    import gitlab as gl_lib

    # 读 .env
    env = {}
    with open("/home/workflow/ReviewAgent/.env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    gitlab_url = env.get("GITLAB_URL", "http://10.20.27.7")
    token = env.get("GITLAB_PERSONAL_ACCESS_TOKEN", "")
    gl = gl_lib.Gitlab(gitlab_url, private_token=token)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 找所有 merged 但 merged_at 为 NULL 的 MR
    null_merged = conn.execute(
        "SELECT project_id, mr_iid FROM mr_activity WHERE state = 'merged' AND merged_at IS NULL"
    ).fetchall()
    # 也找 updated_at 为 NULL 的
    null_updated = conn.execute(
        "SELECT project_id, mr_iid FROM mr_activity WHERE updated_at IS NULL"
    ).fetchall()

    targets = set()
    for r in null_merged:
        targets.add((r["project_id"], r["mr_iid"]))
    for r in null_updated:
        targets.add((r["project_id"], r["mr_iid"]))

    print(f"\nBackfill targets: {len(targets)} MRs")

    def parse_dt(s):
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.isoformat()
        except Exception:
            return None

    fixed = 0
    for pid, iid in sorted(targets):
        try:
            mr = gl.projects.get(pid).mergerequests.get(iid)
        except Exception as e:
            print(f"  !{iid} (project={pid}): API error: {e}")
            continue

        sets, vals = [], []

        # merged_at
        if mr.merged_at:
            row = conn.execute(
                "SELECT merged_at FROM mr_activity WHERE project_id=? AND mr_iid=?",
                (pid, iid),
            ).fetchone()
            if row and row["merged_at"] is None:
                sets.append("merged_at = ?")
                vals.append(parse_dt(mr.merged_at))

        # updated_at
        if mr.updated_at:
            row = conn.execute(
                "SELECT updated_at FROM mr_activity WHERE project_id=? AND mr_iid=?",
                (pid, iid),
            ).fetchone()
            if row and row["updated_at"] is None:
                sets.append("updated_at = ?")
                vals.append(parse_dt(mr.updated_at))

        if sets:
            vals.extend([pid, iid])
            sql = f"UPDATE mr_activity SET {', '.join(sets)} WHERE project_id=? AND mr_iid=?"
            conn.execute(sql, vals)
            conn.commit()
            print(f"  !{iid} (project={pid}): {', '.join(sets)}")
            fixed += 1
        else:
            print(f"  !{iid} (project={pid}): nothing to update")

    conn.close()
    print(f"Backfilled {fixed} MRs")


if __name__ == "__main__":
    fix_stuck_runs()
    backfill_mr_1087()
