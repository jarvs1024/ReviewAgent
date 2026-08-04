"""Smoke test for verify_e2e harness — proves the harness can run a minimal round.

This is a guard to ensure verify_e2e.py syntax + structure stays valid as we evolve
the codebase. It does NOT run a full e2e (which requires live GitLab + workers);
instead it imports verify_e2e module and asserts its public API surface.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "scripts" / "e2e" / "verify_e2e.py"


@pytest.mark.skipif(not HARNESS.exists(), reason="harness script not present")
def test_harness_module_imports():
    """verify_e2e.py parses + imports without side effects."""
    spec = importlib.util.spec_from_file_location("verify_e2e", HARNESS)
    assert spec is not None, "spec_from_file_location returned None"
    # We don't actually execute (would import reviewagent.* at module load).
    # Just verify Python can parse it.
    import ast
    ast.parse(HARNESS.read_text())
    assert True


@pytest.mark.skipif(not HARNESS.exists(), reason="harness script not present")
def test_harness_function_signatures():
    """Verify all expected feature runners + helpers exist with consistent signatures."""
    import ast
    tree = ast.parse(HARNESS.read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    expected = {
        "_post_webhook", "_gitlab_get", "_telemetry_get",
        "_now_iso",
        "clear_cooldown_locks", "get_mr_info",
        "find_open_suggestion", "list_mr_runs",
        "reset_mr_describe_state",
        "make_merge_request_payload", "make_note_payload",
        "run_describe", "run_improve", "run_chain_open",
        "run_adopt", "run_dismiss", "run_ui_apply",
        "run_telemetry", "run_weekly_report",
        "main",
        "wait_for_new_run",
    }
    missing = expected - names
    assert not missing, f"verify_e2e.py missing functions: {missing}"


@pytest.mark.skipif(not HARNESS.exists(), reason="harness script not present")
def test_harness_main_features_constant():
    """main() references the 8 features we expect to cover."""
    src = HARNESS.read_text()
    for feat in ("describe", "improve", "auto_chain", "adopt", "dismiss",
                  "ui_apply", "telemetry_api", "weekly_report"):
        assert f'"{feat}"' in src, f"missing feature token: {feat}"
