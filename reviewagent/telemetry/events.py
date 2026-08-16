"""telemetry 薄 emitter — 失败仅警告，不阻塞主流程."""
from __future__ import annotations

from reviewagent.logging_setup import logger
from reviewagent.telemetry.models import MRRecord, ReviewRun
from reviewagent.telemetry.store import get_store


def emit_mr_activity(mr: MRRecord) -> None:
    try:
        get_store().upsert_mr(mr)
    except Exception as e:
        logger.warning("telemetry.emit_mr_activity failed: {}", e)


def emit_run_started(run: ReviewRun) -> int:
    try:
        return get_store().insert_run(run)
    except Exception as e:
        logger.warning("telemetry.emit_run_started failed: {}", e)
        return -1


def emit_run_finished(
    run_id: int,
    *,
    status: str,
    error: str | None = None,
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_credits: float = 0.0,
    duration_ms: int = 0,
    llm_provider: str | None = None,
) -> None:
    try:
        get_store().finish_run(
            run_id,
            status=status,
            error=error,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_credits=cost_credits,
            duration_ms=duration_ms,
            llm_provider=llm_provider,
        )
    except Exception as e:
        logger.warning("telemetry.emit_run_finished failed: {}", e)


def emit_description_generated(project_id: int, mr_iid: int) -> None:
    try:
        get_store().mark_description_generated(project_id, mr_iid)
    except Exception as e:
        logger.warning("telemetry.emit_description_generated failed: {}", e)