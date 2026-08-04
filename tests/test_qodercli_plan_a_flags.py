"""Plan A (revised): only --max-turns is enabled by default.
--permission-mode is supported but not set (left empty in .env).
"""
import os
import sys
from pathlib import Path

REPO = Path("/Users/jarvs/ReviewAgent")
sys.path.insert(0, str(REPO))

from reviewagent.llm.qodercli_subprocess import _build_cmd_for_test  # type: ignore


def _build(meta_prompt: str = "agent_meta", workdir: str = "/tmp/wt",
           prompt: str = "do thing") -> list[str]:
    return _build_cmd_for_test(
        node="/usr/bin/node",
        script="/path/qodercli.js",
        model="DeepSeek-V4-Flash",
        meta_prompt=meta_prompt,
        workdir=workdir,
        prompt=prompt,
        permission_mode=os.environ.get("QODERCLI_PERMISSION_MODE", ""),
        max_turns=int(os.environ.get("QODERCLI_MAX_TURNS", "0")),
    )


def test_default_no_flags():
    os.environ["QODERCLI_PERMISSION_MODE"] = ""
    os.environ["QODERCLI_MAX_TURNS"] = "0"
    cmd = _build()
    assert "--permission-mode" not in cmd
    assert "--max-turns" not in cmd
    assert cmd[0] == "/usr/bin/node"
    assert cmd[1] == "/path/qodercli.js"
    assert cmd[2] == "-p"
    assert "--model" in cmd and "DeepSeek-V4-Flash" in cmd
    assert "-o" in cmd and "json" in cmd
    assert "--disallowed-tools" in cmd


def test_plan_a_max_turns_only():
    """Plan A (revised): only --max-turns set, no --permission-mode."""
    os.environ["QODERCLI_PERMISSION_MODE"] = ""
    os.environ["QODERCLI_MAX_TURNS"] = "20"
    cmd = _build()
    assert "--permission-mode" not in cmd
    assert "--max-turns" in cmd
    idx = cmd.index("--max-turns")
    assert cmd[idx + 1] == "20"


def test_permission_mode_optional_accept_edits():
    """permission-mode is still wired but opt-in (not set in default .env)."""
    os.environ["QODERCLI_PERMISSION_MODE"] = "accept_edits"
    os.environ["QODERCLI_MAX_TURNS"] = "20"
    cmd = _build()
    assert "--permission-mode" in cmd
    idx = cmd.index("--permission-mode")
    assert cmd[idx + 1] == "accept_edits"
    assert "--max-turns" in cmd
    idx = cmd.index("--max-turns")
    assert cmd[idx + 1] == "20"


def test_permission_mode_optional_bypass_permissions():
    """permission-mode=bypass_permissions is also supported."""
    os.environ["QODERCLI_PERMISSION_MODE"] = "bypass_permissions"
    os.environ["QODERCLI_MAX_TURNS"] = "0"
    cmd = _build()
    assert "--permission-mode" in cmd
    idx = cmd.index("--permission-mode")
    assert cmd[idx + 1] == "bypass_permissions"
    assert "--max-turns" not in cmd
