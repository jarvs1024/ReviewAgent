"""Manual end-to-end probe for the QoderCLI ACP driver.

Boots a real `qodercli --acp` subprocess, runs N concurrent
`session/prompt` calls with different agent names, and asserts each
returns a non-empty response. Prints elapsed wall time so we can sanity
check parallelism. Exits non-zero on any failure.

Run inside the project venv with .env already sourced:

    set -a && source .env && set +a
    .venv/bin/python scripts/probe_qodercli_acp.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

# Make `scripts` and `reviewagent` importable when invoked directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reviewagent.config import config
from reviewagent.llm.qodercli_acp import QoderCLIACPClient
from reviewagent.prompts.loader import PROMPTS_DIR

from scripts.sync_qoder_agents import sync_qoder_agents


CONCURRENCY = 3
PROMPT_TEMPLATE = "Reply with the integer {idx} wrapped in {{...}} JSON and nothing else."


def main() -> int:
    workdir = Path.cwd()
    agents_dir = workdir / ".qoder" / "agents"
    sync_qoder_agents(PROMPTS_DIR, agents_dir)

    node = config.qodercli_node_path or "node"
    script = config.qodercli_js_path
    model = config.qodercli_model
    print(
        f"[probe] node={node} script={script} model={model} "
        f"workdir={workdir} agents_dir={agents_dir}"
    )

    client = QoderCLIACPClient.bootstrap(
        node=node,
        script=script,
        model=model,
        extra_args=list(config.qodercli_acp_extra_args)
        + ["--setting-sources", "project,user,local"],
        workdir=workdir,
    )
    try:
        caps = client.initialize(
            client_info={"name": "probe"}, capabilities={}
        )
        print(f"[probe] agentCapabilities={json.dumps(caps)[:200]}")

        barrier = threading.Barrier(CONCURRENCY)
        results: list[str] = []
        errors: list[str] = []
        lock = threading.Lock()

        def _run(idx: int) -> None:
            try:
                sid = client.session_new(cwd=workdir)
                barrier.wait()
                client.session_prompt(
                    sid, PROMPT_TEMPLATE.format(idx=idx), timeout=180,
                )
                text = client.collect_message(sid)
                with lock:
                    results.append(f"{idx}:{text}")
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(f"{idx}: {e!r}")

        threads = [
            threading.Thread(target=_run, args=(i,)) for i in range(CONCURRENCY)
        ]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=240)
        elapsed = time.time() - start

        print(f"[probe] elapsed={elapsed:.2f}s")
        for r in sorted(results):
            print(f"[probe] result {r[:160]}")
        for e in errors:
            print(f"[probe] ERROR {e}", file=sys.stderr)

        if errors or len(results) != CONCURRENCY:
            print(f"[probe] FAIL: results={len(results)} errors={len(errors)}")
            return 1
        # Each result must be non-empty.
        if not all(":" in r and len(r.split(":", 1)[1].strip()) > 0 for r in results):
            print("[probe] FAIL: empty results")
            return 1
        print("[probe] PASS")
        return 0
    finally:
        client.shutdown()


if __name__ == "__main__":
    sys.exit(main())
