"""Sync GitLab project webhook secret to match .env's GITLAB_WEBHOOK_SECRET.

Why this exists:
    GitLab stores a per-hook token. ReviewAgent verifies `X-Gitlab-Token`
    against `GITLAB_WEBHOOK_SECRET` in .env. If they ever drift (e.g. .env
    rotated, or hook was created before secret was finalized), all webhook
    deliveries return 401 and the bot stops reacting to /adopt, /dismiss,
    MR events, etc. without any obvious error in the server log.

This script re-PUTs every hook in the target project with the current
secret so they line up.

Usage:
    python scripts/sync_webhook.py --project-id 34
    python scripts/sync_webhook.py --project-id 34 --dry-run
    python scripts/sync_webhook.py --project-id 34 --only-url host.docker.internal:3000/webhook

Env (loaded from .env if present):
    GITLAB_URL
    GITLAB_PERSONAL_ACCESS_TOKEN
    GITLAB_WEBHOOK_SECRET
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests


def _load_env() -> None:
    """Best-effort load of .env into os.environ (no external deps)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    _load_env()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=int, required=True, help="GitLab project ID (numeric)")
    parser.add_argument(
        "--only-url",
        default=None,
        help="Only update hooks whose URL matches this substring (e.g. 'host.docker.internal:3000/webhook')",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without changing anything")
    args = parser.parse_args()

    gitlab_url = os.environ.get("GITLAB_URL", "").rstrip("/")
    pat = os.environ.get("GITLAB_PERSONAL_ACCESS_TOKEN", "")
    secret = os.environ.get("GITLAB_WEBHOOK_SECRET", "")

    if not gitlab_url or not pat or not secret:
        print("error: GITLAB_URL / GITLAB_PERSONAL_ACCESS_TOKEN / GITLAB_WEBHOOK_SECRET must be set", file=sys.stderr)
        return 2

    headers = {"PRIVATE-TOKEN": pat, "Content-Type": "application/json"}

    list_url = f"{gitlab_url}/api/v4/projects/{args.project_id}/hooks"
    resp = requests.get(list_url, headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"error: GET {list_url} -> {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return 1
    hooks = resp.json()
    if not isinstance(hooks, list):
        print(f"error: expected JSON list, got: {hooks}", file=sys.stderr)
        return 1

    print(f"found {len(hooks)} hook(s) in project {args.project_id}")

    updated = 0
    skipped = 0
    for hook in hooks:
        url = hook.get("url", "")
        if args.only_url and args.only_url not in url:
            continue
        if args.dry_run:
            print(f"[dry-run] would PUT hook {hook['id']} ({url}) with current secret")
            updated += 1
            continue
        put_url = f"{gitlab_url}/api/v4/projects/{args.project_id}/hooks/{hook['id']}"
        body = {
            "url": url,
            "token": secret,
        }
        for ev in (
            "push_events",
            "issues_events",
            "confidential_issues_events",
            "merge_requests_events",
            "tag_push_events",
            "note_events",
            "confidential_note_events",
            "pipeline_events",
            "wiki_page_events",
            "job_events",
            "releases_events",
            "enable_ssl_verification",
        ):
            if ev in hook:
                body[ev] = hook[ev]
        r = requests.put(put_url, headers=headers, json=body, timeout=15)
        if r.status_code in (200, 201):
            print(f"OK   hook {hook['id']:>3}  {url}")
            updated += 1
        else:
            print(f"FAIL hook {hook['id']:>3}  {url}  -> {r.status_code} {r.text[:120]}")
            skipped += 1

    print(f"\ndone: {updated} updated, {skipped} failed (dry_run={args.dry_run})")
    return 0 if skipped == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
