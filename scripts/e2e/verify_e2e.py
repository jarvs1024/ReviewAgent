"""兼容入口：旧的 `verify_e2e.py` 命令转发到统一入口 `run_e2e.py`.

等价于 `python scripts/e2e/run_e2e.py --tier full ...`（保留旧 flag `--feature`）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.e2e.run_e2e import main  # noqa: E402

__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], default_tier="full"))
