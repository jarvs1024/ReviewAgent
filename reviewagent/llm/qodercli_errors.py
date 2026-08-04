"""Shared qodercli exception hierarchy.

All qodercli-related providers (ACP driver, subprocess driver, future
drivers) raise exceptions rooted at :class:`QoderCLIError`. This lets
upstream callers write ``except QoderCLIError`` and catch failures from
any driver without naming the concrete subclass.

Hierarchy::

    QoderCLIError(RuntimeError)
    ├── QoderCLITimeoutError    — wall-clock budget exhausted
    ├── QoderCLIOutputError     — stdout empty / not JSON / agent missing
    └── QoderCLIACPError        — base for ACP-driver-specific failures
        ├── QoderCLIAuthError
        ├── QoderCLITimeoutError (ACP variant; kept for back-compat)
        └── QoderCLIProtocolError

The ACP variants shadow :class:`QoderCLITimeoutError` on purpose --
``__init__.py`` re-exports the canonical (provider-level) one. The
ACP timeout class still inherits via :class:`QoderCLIACPError`, so
``except QoderCLIError`` catches both.
"""

from __future__ import annotations


class QoderCLIError(RuntimeError):
    """Base class for all qodercli driver failures."""


class QoderCLITimeoutError(QoderCLIError):
    """qodercli task exceeded its wall-clock budget."""


class QoderCLIOutputError(QoderCLIError):
    """qodercli stdout could not be parsed as the expected JSON shape."""


__all__ = ["QoderCLIError", "QoderCLITimeoutError", "QoderCLIOutputError"]
