"""supersede_stale_in_cohort 回归测试.

Why: process_adopt / mark_suggestion_applied_by_diff 命中单条 applied 后,
同 cohort 旧条仍 open 会让 build_overview 双发. helper 把同 cohort
除 keep_note_id 外的所有 open/resolved/dismissed 标 superseded.
"""
from pathlib import Path
import pytest

from reviewagent.telemetry.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "telemetry.db")


def _seed(store, *, note_id, cohort_key, state="open"):
    store.record_suggestion(
        project_id=34, mr_iid=252, note_id=note_id,
        file_path=f"services/test_{note_id[:8]}.py",
        target_line=10, existing_code="x = 1", improved_code="x = 2",
        header="test", severity="medium", head_sha="abc1234",
        fingerprint=note_id, cohort_key=cohort_key,
    )
    if state != "open":
        store.update_suggestion_state(note_id, state)


def test_supersede_stale_in_cohort_marks_other_states(store):
    _seed(store, note_id="aaaaaaaa01", cohort_key="ck_a", state="open")
    _seed(store, note_id="aaaaaaaa02", cohort_key="ck_a", state="resolved")
    _seed(store, note_id="aaaaaaaa03", cohort_key="ck_a", state="dismissed")
    _seed(store, note_id="aaaaaaaa04", cohort_key="ck_b", state="open")

    superseded = store.supersede_stale_in_cohort(
        project_id=34, mr_iid=252, cohort_key="ck_a", keep_note_id="aaaaaaaa01",
    )
    assert sorted(superseded) == ["aaaaaaaa02", "aaaaaaaa03"]
    states = {nid: store.get_suggestion_by_note_id(nid)["state"]
              for nid in ("aaaaaaaa01", "aaaaaaaa02", "aaaaaaaa03", "aaaaaaaa04")}
    assert states["aaaaaaaa01"] == "open"
    assert states["aaaaaaaa02"] == "superseded"
    assert states["aaaaaaaa03"] == "superseded"
    assert states["aaaaaaaa04"] == "open"


def test_supersede_stale_in_cohort_empty(store):
    out = store.supersede_stale_in_cohort(
        project_id=34, mr_iid=252, cohort_key="ck_empty", keep_note_id="none",
    )
    assert out == []
