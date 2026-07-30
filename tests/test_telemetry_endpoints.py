"""端到端校验 telemetry 端点 + store 新方法."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from reviewagent.telemetry.models import MRRecord
from reviewagent.telemetry.store import Store


def _make_store() -> tuple[Store, TemporaryDirectory]:
    tmp = TemporaryDirectory()
    return Store(Path(tmp.name) / "telemetry.db"), tmp


def test_record_suggestion_persists_extended_fields():
    store, tmp = _make_store()
    try:
        store.upsert_mr(MRRecord(34, 900, "extended", "alice", "feature", "main", "opened"))
        sid = store.record_suggestion(
            project_id=34, mr_iid=900, note_id="note-1",
            file_path="a.py", target_line=8,
            existing_code="x = 1", improved_code="x = 2",
            header="h", severity="high", head_sha="abc",
            rule_keys=["ZLG-RULE-NO-LOG-EXC", "ZLG-RULE-SQL-INJECT"],
            importance=8, label="security", one_sentence_summary="参数化查询",
        )
        row = store.get_suggestion_by_note_id("note-1")
        assert row["rule_keys"].split(",") == ["ZLG-RULE-NO-LOG-EXC", "ZLG-RULE-SQL-INJECT"]
        assert row["importance"] == 8
        assert row["label"] == "security"
        assert row["one_sentence_summary"] == "参数化查询"
        assert row["cohort_key"] is None
        assert row["state"] == "open"
    finally:
        tmp.cleanup()


def test_dismissed_reason_preserved_with_actor():
    store, tmp = _make_store()
    try:
        store.upsert_mr(MRRecord(34, 901, "d", "alice", "f", "m", "opened"))
        store.record_suggestion(
            project_id=34, mr_iid=901, note_id="note-2",
            file_path="a.py", target_line=1, header="h", severity="medium", head_sha="sha",
            rule_keys=["ZLG-RULE-NO-LOG-EXC"],
        )
        store.update_suggestion_state(
            "note-2", "dismissed", actor_username="bob", dismissed_reason="误报",
        )
        row = store.get_suggestion_by_note_id("note-2")
        assert row["state"] == "dismissed"
        assert row["dismissed_by"] == "bob"
        assert row["dismissed_reason"] == "误报"
        assert row["dismissed_at"] is not None
    finally:
        tmp.cleanup()


def test_dismissals_by_rule_aggregates():
    store, tmp = _make_store()
    try:
        store.upsert_mr(MRRecord(34, 902, "d", "alice", "f", "m", "opened"))
        for i, key in enumerate(["ZLG-RULE-NO-LOG-EXC", "ZLG-RULE-NO-LOG-EXC", "ZLG-RULE-SQL-INJECT"]):
            store.record_suggestion(
                project_id=34, mr_iid=902, note_id=f"n-{i}",
                file_path="a.py", target_line=i+1, header="h", severity="low", head_sha="sha",
                rule_keys=[key],
            )
            store.update_suggestion_state(
                f"n-{i}", "dismissed", actor_username="bob",
                dismissed_reason=("误报" if i == 0 else "项目不需要"),
            )
        bucket = store.dismissals_by_rule(project_id=34)
        keys = {row["rule_key"]: row for row in bucket}
        assert keys["ZLG-RULE-NO-LOG-EXC"]["dismissal_count"] == 2
        reason_map = {r["reason"]: r["count"] for r in keys["ZLG-RULE-NO-LOG-EXC"]["reasons"]}
        assert reason_map["误报"] == 1
        assert reason_map["项目不需要"] == 1
        assert "ZLG-RULE-SQL-INJECT" in keys
        assert store.distinct_rule_keys(project_id=34) == [
            "ZLG-RULE-NO-LOG-EXC", "ZLG-RULE-SQL-INJECT",
        ]
    finally:
        tmp.cleanup()
