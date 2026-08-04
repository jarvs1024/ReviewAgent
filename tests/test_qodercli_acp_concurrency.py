"""Public surface: re-exports + concurrent chat() under one client."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import reviewagent.llm as llm
from reviewagent.llm.qodercli_acp import QoderCLIACPClient


def test_qodercli_acp_client_is_reexported() -> None:
    assert llm.QoderCLIACPClient is QoderCLIACPClient


def test_concurrent_chat_resolves_three_sessions(tmp_path: Path) -> None:
    transport = MagicMock()
    client = QoderCLIACPClient(
        node="/usr/bin/node",
        script="/opt/q.js",
        model="DeepSeek-V4-Flash",
        extra_args=[],
        transport=transport,
    )
    client._workdir = tmp_path
    client._sem = threading.Semaphore(4)

    delays = [0.2, 0.05, 0.1]
    fake_results_by_agent = {
        f"agent-{i}": {"stop_reason": "end_turn", "text": '{"id": ' + str(i) + "}"}
        for i in range(3)
    }
    sid_counter = {"i": 0}
    sid_lock = threading.Lock()

    def _fake_prompt(sid, text, **kwargs):
        # text == f"task {idx}"; recover idx deterministically per call
        idx = int(text.split()[-1])
        time.sleep(delays[idx])
        return fake_results_by_agent[f"agent-{idx}"]

    def _fake_new(**kwargs):
        with sid_lock:
            sid_counter["i"] += 1
            return f"sess-{sid_counter['i']}"

    with patch.object(client, "session_new", side_effect=_fake_new), \
         patch.object(client, "session_prompt", side_effect=_fake_prompt):
        results: list[tuple[str, dict]] = []
        result_lock = threading.Lock()
        barrier = threading.Barrier(3)

        def _run(idx: int) -> None:
            agent_name = f"agent-{idx}"
            barrier.wait()
            r = client.chat(
                agent=agent_name,
                prompt=f"task {idx}",
                files=[],
                timeout=5.0,
                max_concurrent_sessions=4,
                session_reuse_window=0,
            )
            with result_lock:
                results.append((agent_name, r))

        threads = [threading.Thread(target=_run, args=(i,), name=f"t{i}") for i in range(3)]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)
        elapsed = time.time() - start

    # Sort by agent name to make assertions independent of thread scheduling.
    results.sort(key=lambda pair: pair[0])
    assert [r["text"] for _, r in results] == ['{"id": 0}', '{"id": 1}', '{"id": 2}']
    # Three different session IDs were issued (no reuse with window=0).
    sids = sorted([sid for sid in sid_counter and sid_counter["i"] >= 3] if False else [])
    # sid_counter is a dict; sids collected via the lock — checking it advanced to 3:
    assert sid_counter["i"] == 3
    # Truly parallel: elapsed ≈ max(delays) = 0.2s, not sum (0.35s).
    assert elapsed < 0.3
