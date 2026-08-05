"""Test for GitLab webhook created_at parsing fix.

Bug found 2026-08-06: GitLab MR hook returns created_at in format
'2026-08-05 16:57:43 UTC' (newer GitLab versions), but _parse_dt
only handled ISO 8601 format. Result: mr_activity.created_at was
always None, breaking the "data collection start time" field.

Fix: _parse_dt now also handles 'YYYY-MM-DD HH:MM:SS UTC' format.
"""
from reviewagent.telemetry.models import _parse_dt


def test_parse_dt_iso8601_z():
    """ISO 8601 with Z suffix."""
    dt = _parse_dt("2024-01-15T10:30:00.000Z")
    assert dt is not None
    assert dt.year == 2024 and dt.month == 1 and dt.day == 15
    assert dt.tzinfo is not None


def test_parse_dt_iso8601_offset():
    """ISO 8601 with +00:00 offset."""
    dt = _parse_dt("2024-01-15T10:30:00.000+00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_dt_gitlab_new_format():
    """New GitLab format: 'YYYY-MM-DD HH:MM:SS UTC'."""
    dt = _parse_dt("2026-08-05 16:57:43 UTC")
    assert dt is not None, f"new format should parse, got None"
    assert dt.year == 2026 and dt.month == 8 and dt.day == 5
    assert dt.hour == 16 and dt.minute == 57 and dt.second == 43
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_parse_dt_none_and_empty():
    """None and empty string return None."""
    assert _parse_dt(None) is None
    assert _parse_dt("") is None


def test_parse_dt_invalid_returns_none():
    """Invalid format returns None (doesn't raise)."""
    assert _parse_dt("not a date") is None
