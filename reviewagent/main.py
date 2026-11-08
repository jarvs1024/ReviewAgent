"""FastAPI app 入口.

启动:
    uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000 --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from reviewagent import bootstrap
from reviewagent.api.router import router as telemetry_router
from reviewagent.logging_setup import logger
from reviewagent.telemetry.store import get_store
from reviewagent.webhook.router import router as webhook_router

__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动装配：加载/激活 profile、绑定 data_dir、配置 fingerprint 日志.
    # setup_logging 在 bootstrap.startup 内部（幂等），REVIEW_PROFILE 非法时
    # 这里直接抛错，uvicorn 会带着原始消息拒绝启动.
    bootstrap.startup("api")
    logger.info("reviewagent.starting version={}", __version__)
    try:
        get_store()
        logger.info("reviewagent.sqlite ok path={}", get_store().path)
    except Exception as e:
        logger.warning("reviewagent.sqlite init failed: {}", e)
    yield
    logger.info("reviewagent.shutdown")


app = FastAPI(
    title="ReviewAgent",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(webhook_router, tags=["webhook"])
app.include_router(telemetry_router)


def _register_metric_help() -> None:
    """注册 metric 的 HELP 文本 — 启动时一次性调用, 让 /metrics 输出更友好."""
    from reviewagent.metrics import metrics as _metrics

    helps = {
        "reviewagent_improve_file_limit_total":
            "Number of improve runs that hit IMPROVE_FULL_FILES truncation.",
        "reviewagent_improve_files_skipped_total":
            "Number of files dropped because of IMPROVE_FULL_FILES.",
        "reviewagent_webhook_received_total":
            "Webhook events received, labeled by object_kind.",
        "reviewagent_webhook_skipped_total":
            "Webhook events skipped, labeled by reason (cooldown, bot_self, ...)." ,
        "reviewagent_chain_enqueued_total":
            "MR command chains enqueued for execution.",
        "reviewagent_lock_diff_head_total":
            "diff_head SHA lock acquisitions; kind=acquired|contended.",
        "reviewagent_lock_chain_total":
            "Per-MR chain lock lifecycle; kind=acquired|timeout|released|mismatch.",
        "reviewagent_suggestion_supersede_total":
            "Suggestions marked state=superseded due to head_sha change.",
        "reviewagent_llm_provider_initialized_total":
            "LLM provider instances initialized, labeled by provider name.",
    }
    for name, help_text in helps.items():
        _metrics.register_help(name, help_text)


_register_metric_help()


@app.get("/metrics", tags=["meta"], response_class=PlainTextResponse)
async def metrics_endpoint() -> Response:
    """Prometheus text exposition format — 轻量级, 仅 counter + gauge."""
    from reviewagent.metrics import format_prometheus as _fmt
    # 注册幂等: 即使进程被测试 reset 过也能保证所有 HELP 在 /metrics 出现.
    _register_metric_help()
    body = _fmt()
    return PlainTextResponse(
        content=body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )


@app.get("/readyz", tags=["meta"])
async def readyz() -> JSONResponse:
    """运行时就绪探针：profile/RuleSet/instance + SQLite + Redis 全部通过才 200.

    探针含同步 SQLite / Redis ping，放线程池执行，避免阻塞事件循环。
    """
    ok, reasons = await run_in_threadpool(bootstrap.runtime_readiness)
    if not ok:
        return JSONResponse({"status": "unready", "reasons": reasons}, status_code=503)
    body: dict[str, object] = {"status": "ok"}
    state = bootstrap.started()
    if state is not None:
        body["profile"] = state.profile_name
        body["profile_fingerprint"] = state.profile_fingerprint
    return JSONResponse(body)


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {
        "service": "reviewagent",
        "version": __version__,
        "endpoints": {
            "webhook": "POST /webhook",
            "health": "GET /health",
            "readyz": "GET /readyz",
            "docs": "GET /docs",
        },
    }
