"""qodercli exception hierarchy.

Subprocess driver exceptions all derive from :class:`QoderCLIError`,
so callers can write ``except QoderCLIError`` without naming the
concrete subclass. As of 2026-08-05 only the subprocess driver remains
(ACP removed — see ``qodercli_provider`` module docstring).
"""

from __future__ import annotations


class QoderCLIError(RuntimeError):
    """Base class for all qodercli driver failures."""


class QoderCLITimeoutError(QoderCLIError):
    """qodercli task exceeded its wall-clock budget."""


class QoderCLIOutputError(QoderCLIError):
    """qodercli stdout could not be parsed as the expected JSON shape."""


class QoderCLIDaemonError(QoderCLIError):
    """opencode/qodercli daemon is unreachable (health-check failed)."""


__all__ = [
    "QoderCLIError",
    "QoderCLITimeoutError",
    "QoderCLIOutputError",
    "QoderCLIDaemonError",
]
