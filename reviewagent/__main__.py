"""`python -m reviewagent` 入口（等同 `python -m reviewagent.cli`）."""
from __future__ import annotations

from reviewagent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
