"""FastAPI app 入口.

启动:
    uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000 --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from reviewagent.gitlab.client import GitLabError
from reviewagent.logging_setup import logger, setup_logging
from reviewagent.telemetry.store import get_store
from reviewagent.webhook.router import router as webhook_router


__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
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


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {
        "service": "reviewagent",
        "version": __version__,
        "endpoints": {
            "webhook": "POST /webhook",
            "health": "GET /health",
            "docs": "GET /docs",
        },
    }