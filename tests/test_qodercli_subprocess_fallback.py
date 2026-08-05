"""Subprocess driver: run_subprocess parsing + JSON edge cases."""

from __future__ import annotations

import os
import importlib
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from reviewagent.llm.qodercli_subprocess import run_subprocess
from reviewagent.llm.qodercli_provider import QoderCLIOutputError


def test_subprocess_path_invokes_node_script(tmp_path: Path) -> None:
    captured = {}
    fake_proc = MagicMock()
    fake_proc.stdout = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "{\"ok\": true}",
        "stop_reason": "end_turn",
        "duration_ms": 1234,
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })
    fake_proc.stderr = ""
    fake_proc.returncode = 0

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return fake_proc

    with patch("subprocess.run", side_effect=_fake_run):
        result = run_subprocess(
            agent="improve",
            prompt="review",
            workdir=tmp_path,
            files=[],
            timeout=120,
            tolerant_markdown=False,
        )
    assert "--append-system-prompt" in captured["cmd"]
    assert "--model" in captured["cmd"]
    assert captured["cmd"][-1] == "review"
    assert result.data == {"ok": True}
    assert result.provider == "qodercli"
    assert result.model  # filled from config
    assert result.duration_ms >= 0


def test_subprocess_path_tolerant_markdown(tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = "not json"
    fake_proc.stderr = ""
    fake_proc.returncode = 0

    def _fake_run(cmd, **kwargs):
        return fake_proc

    with patch("subprocess.run", side_effect=_fake_run):
        result = run_subprocess(
            agent="improve",
            prompt="x",
            workdir=tmp_path,
            files=[],
            timeout=30,
            tolerant_markdown=True,
        )
    assert result.data == {}
    assert result.raw_output == "not json"


def test_subprocess_path_raises_on_empty_stdout(tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = ""
    fake_proc.stderr = "boom"
    fake_proc.returncode = 0
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(QoderCLIOutputError, match="empty"):
            run_subprocess(agent="improve", prompt="x", workdir=tmp_path, files=[], timeout=30, tolerant_markdown=False)


@pytest.mark.parametrize(
    "inner, expected",
    [
        (
            '{"title":"x","description_md":"line 1\nline 2"}',
            {"title": "x", "description_md": "line 1\nline 2"},
        ),
        ('{"ok":true}\nJSON generated.', {"ok": True}),
    ],
)
def test_subprocess_path_recovers_common_llm_json_variants(
    tmp_path: Path, inner: str, expected: dict
) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": inner,
        "stop_reason": "end_turn",
        "usage": {},
    })
    fake_proc.stderr = ""
    fake_proc.returncode = 0

    with patch("subprocess.run", return_value=fake_proc):
        result = run_subprocess(
            agent="describe",
            prompt="x",
            workdir=tmp_path,
            files=[],
            timeout=30,
            tolerant_markdown=False,
        )

    assert result.data == expected


# ---------- _resolve_paths / _resolve_script_path fallback ----------

class TestResolveScriptPath:
    """`QODERCLI_JS_PATH` 是绝对路径最佳来源，缺失时降级到 `$(which qodercli)`.

    设计动机：避免在 .env 里 hardcode 一条像
      `/Users/foo/.nvm/versions/node/v22.22.2/lib/node_modules/@qoder-ai/qodercli/bundle/qodercli.js`
    这样的机器专属路径 — 不同机器 / 不同 node 版本都会失效。
    """

    def setup_method(self):
        """每次测试重置 config singleton + qodercli_subprocess module."""
        import importlib
        import reviewagent.config as ccfg
        import reviewagent.llm.qodercli_subprocess as qsp
        self._ccfg = ccfg
        self._qsp = qsp
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("QODERCLI_NODE_PATH", "QODERCLI_JS_PATH")
        }

    def teardown_method(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # restore config
        self._ccfg.config = self._ccfg.Config.from_env()
        importlib.reload(self._qsp)

    def test_env_existing_path_wins(self):
        """env 设到真实存在路径 -> 用 env."""
        import os
        from pathlib import Path
        real = Path(os.path.realpath(os.path.expanduser("$(which qodercli)"))) \
            if False else __import__("shutil").which("qodercli")
        # 直接构造一个真实存在的临时文件作为 env 值
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(b"// stub")
            env_path = f.name
        try:
            os.environ["QODERCLI_NODE_PATH"] = "/usr/bin/env"
            os.environ["QODERCLI_JS_PATH"] = env_path
            self._ccfg.config = self._ccfg.Config.from_env()
            importlib.reload(self._qsp)
            node, script, _ = self._qsp._resolve_paths(None, None, None)
            assert script == env_path
            assert node == "/usr/bin/env"
        finally:
            os.unlink(env_path)

    def test_env_broken_path_falls_back_to_path(self):
        """env 路径不存在 -> 自动 recover 到 PATH 上的 qodercli."""
        import os
        os.environ["QODERCLI_JS_PATH"] = "/nonexistent/qodercli.js"
        self._ccfg.config = self._ccfg.Config.from_env()
        importlib.reload(self._qsp)
        _, script, _ = self._qsp._resolve_paths(None, None, None)
        assert script != "/nonexistent/qodercli.js", \
            "broken env path should fall back to PATH"
        import os.path
        assert os.path.isfile(script)

    def test_path_fallback_when_env_empty(self):
        """env 整段没设 -> 走 which node / which qodercli."""
        import os
        os.environ.pop("QODERCLI_NODE_PATH", None)
        os.environ.pop("QODERCLI_JS_PATH", None)
        self._ccfg.config = self._ccfg.Config.from_env()
        importlib.reload(self._qsp)
        node, script, _ = self._qsp._resolve_paths(None, None, None)
        import os.path
        assert os.path.isfile(node), f"node not found: {node}"
        assert os.path.isfile(script), f"script not found: {script}"

    def test_explicit_args_override_everything(self):
        """显式 node=/script= 参数始终优先于 env+PATH."""
        self._ccfg.config = self._ccfg.Config.from_env()
        importlib.reload(self._qsp)
        node, script, model = self._qsp._resolve_paths("/x/n", "/x/s.js", "M")
        assert (node, script, model) == ("/x/n", "/x/s.js", "M")

    def test_path_missing_raises_clear_error(self, monkeypatch):
        """PATH 上既没 node 也没 qodercli -> QoderCLIError，错误文案指引去配 env."""
        # 打补丁: shutil.which 全部返回 None
        monkeypatch.setattr(self._qsp.shutil, "which", lambda _: None)
        os.environ.pop("QODERCLI_NODE_PATH", None)
        os.environ.pop("QODERCLI_JS_PATH", None)
        self._ccfg.config = self._ccfg.Config.from_env()
        importlib.reload(self._qsp)
        with pytest.raises(Exception) as ei:
            self._qsp._resolve_paths(None, None, None)
        msg = str(ei.value)
        assert "PATH" in msg or "env" in msg.lower(), \
            f"error msg should mention PATH/env, got: {msg!r}"
