"""Test the launchd plist is valid and configured correctly.

Background:
    scripts/com.jarvs.reviewagent.reconciler.plist 是 launchd agent 配置,
    调起 reviewagent/reconciler/loop.py 每 60s 跑一次.
    测试 plist 解析 OK, 路径正确, StartInterval=60.
"""
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLIST_PATH = REPO_ROOT / "scripts" / "com.jarvs.reviewagent.reconciler.plist"
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_reconciler.sh"
LOOP_PATH = REPO_ROOT / "reviewagent" / "reconciler" / "loop.py"


def test_plist_exists():
    assert PLIST_PATH.exists(), f"plist missing: {PLIST_PATH}"


def test_plist_valid():
    """plist 必须能用 plutil 解析."""
    result = subprocess.run(
        ["plutil", "-lint", str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"plutil failed: {result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout


def test_plist_required_keys():
    """launchd plist 必须含 Label / ProgramArguments / StartInterval."""
    with PLIST_PATH.open("rb") as f:
        plist = plistlib.load(f)
    assert "Label" in plist
    assert plist["Label"] == "com.jarvs.reviewagent.reconciler"
    assert "ProgramArguments" in plist
    assert len(plist["ProgramArguments"]) >= 1
    assert "StartInterval" in plist
    assert plist["StartInterval"] == 60, f"StartInterval={plist['StartInterval']}, expected 60"


def test_plist_program_points_to_run_script():
    """plist ProgramArguments[0] 必须指向 scripts/run_reconciler.sh."""
    with PLIST_PATH.open("rb") as f:
        plist = plistlib.load(f)
    script = plist["ProgramArguments"][0]
    assert script.endswith("scripts/run_reconciler.sh"), f"unexpected: {script}"
    # 检查对应文件存在
    p = Path(script)
    assert p.exists(), f"run_reconciler.sh missing: {p}"


def test_plist_working_directory_is_repo():
    """WorkingDirectory 必须指向 repo 根目录."""
    with PLIST_PATH.open("rb") as f:
        plist = plistlib.load(f)
    wd = plist.get("WorkingDirectory")
    assert wd == str(REPO_ROOT), f"WorkingDirectory={wd}"


def test_run_reconciler_script_executable():
    """scripts/run_reconciler.sh 必须可执行 (launchd launchctl 会拒绝非可执行)."""
    assert SCRIPT_PATH.exists(), f"missing: {SCRIPT_PATH}"
    import os
    import stat
    mode = SCRIPT_PATH.stat().st_mode
    assert mode & stat.S_IXUSR, f"run_reconciler.sh not executable: {oct(mode)}"


def test_run_reconciler_calls_loop_module():
    """scripts/run_reconciler.sh 必须调 reviewagent.reconciler.loop."""
    content = SCRIPT_PATH.read_text()
    assert "reviewagent.reconciler.loop" in content, (
        f"run_reconciler.sh doesn't call loop module:\n{content}"
    )


def test_loop_module_cli_entry():
    """reviewagent/reconciler/loop.py 必须支持 `python -m reviewagent.reconciler.loop` 调用."""
    content = LOOP_PATH.read_text()
    assert 'if __name__ == "__main__":' in content
    # 必须支持 --project-id 参数
    assert '"--project-id"' in content
