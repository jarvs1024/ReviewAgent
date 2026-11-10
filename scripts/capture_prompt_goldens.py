# scripts/capture_prompt_goldens.py
"""从重构前实现捕获 prompt 基线。仅在 Task 0 运行一次；结果提交到 tests/golden/。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

_DATA = tempfile.mkdtemp(prefix="ra-golden-")
os.environ.setdefault("REVIEWAGENT_DATA_DIR", _DATA)
os.environ.setdefault("GITLAB_URL", "http://localhost")
os.environ.setdefault("GITLAB_PERSONAL_ACCESS_TOKEN", "x")
os.environ.setdefault("GITLAB_WEBHOOK_SECRET", "x")

import dataclasses  # noqa: E402

from reviewagent.commands import improve as improve_module  # noqa: E402
from reviewagent.commands.improve import ImproveCommand  # noqa: E402

# 与 tests/golden/test_prompt_golden.py 的 _RENDER_CONFIG 保持一致:
# 基线必须由 schema 默认值渲染, 不受本地 config/profiles/*/review.yaml 影响.
_RENDER_CONFIG = {
    "improve_partial_context_lines": 20,
    "improve_patch_context_lines": 3,
    "improve_full_files": 20,
    "improve_clip_diff_lines": 800,
    "improve_full_source_max_bytes": 0,
}
from reviewagent.config import current  # noqa: E402

improve_module.config = dataclasses.replace(current(), **_RENDER_CONFIG)

DIFF = (
    "diff --git a/core.py b/core.py\n"
    "--- a/core.py\n+++ b/core.py\n"
    "@@ -1,3 +1,4 @@\n import os\n+import json\n def f():\n-    return 1\n+    return 2\n"
)
SOURCE = "import os\nimport json\ndef f():\n    return 2\n"
VALID = {3, 5, 6}
REFS = [{"file": "tests/test_core.py", "line": 12, "ident": "f", "content": "assert f() == 2"}]
PRIOR = [{"severity": "high", "header": "资源未关闭", "label": "potential bug"}]


def _cmd(worktree: Path, repo_context: str = "") -> ImproveCommand:
    (worktree / "core.py").write_text(SOURCE)
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "test_core.py").write_text("from core import f\nassert f() == 2\n")
    (worktree / ".diff").write_text(DIFF)
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 1
    cmd.mr_iid = 1
    cmd.ws = SimpleNamespace(worktree=worktree, diff_file=worktree / ".diff")
    cmd.repo_context = repo_context
    cmd._last_oc_result = None
    return cmd


def main() -> None:
    out = Path("tests/golden/prompts/ssd-python-autotest")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        cmd = _cmd(wt)
        cases = {
            "chunk_full.txt": cmd._build_chunk_prompt("core.py", DIFF, VALID, cmd.ws, cross_file_refs=[], strategy="full"),
            "chunk_partial_refs.txt": cmd._build_chunk_prompt("core.py", DIFF, VALID, cmd.ws, cross_file_refs=REFS, strategy="partial"),
            "chunk_patch_prior.txt": cmd._build_chunk_prompt("core.py", DIFF, VALID, cmd.ws, cross_file_refs=[], strategy="patch", prior_issues=PRIOR),
            "batch.txt": cmd._build_batch_prompt(
                ["core.py"], {"core.py": DIFF}, {"core.py": VALID}, cmd.ws,
                {"core.py": REFS}, {"core.py": "patch"}, {"core.py": PRIOR},
            ),
            "user_prompt.txt": cmd._build_user_prompt(),
        }
        repo_cmd = _cmd(wt, repo_context="## AGENTS.md\n\n- 必须使用 with\n")
        cases["chunk_repo_ctx.txt"] = repo_cmd._build_chunk_prompt(
            "core.py", DIFF, VALID, repo_cmd.ws, cross_file_refs=[], strategy="full",
        )
        manifest = {}
        for name, text in cases.items():
            data = text.encode("utf-8")
            (out / name).write_bytes(data)
            manifest[name] = hashlib.sha256(data).hexdigest()
    Path("tests/golden/manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
