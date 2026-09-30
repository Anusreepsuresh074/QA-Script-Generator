"""FastAPI entrypoint: health checks + Slack Socket Mode lifecycle."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.config import get_settings
from app import slack as slack_pkg
from app.services.bug_processor import BugProcessor
from app.slack.slack_handler import start_socket_mode_processor, stop_socket_mode
from app.utils.logger import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: logging, BugProcessor, then Slack Socket Mode in a background task
    so HTTP `/health` is available even while Slack is connecting or retrying.

    Shutdown: cancel the Slack task, close the socket client, and release Jira HTTP.
    """
    settings = get_settings()
    setup_logging(settings.log_level)
    processor = BugProcessor(settings)
    app.state.processor = processor

    async def slack_lifecycle() -> None:
        try:
            await start_socket_mode_processor(processor)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Slack Socket Mode failed to start or crashed")

    slack_task = asyncio.create_task(slack_lifecycle())
    app.state.slack_task = slack_task
    logger.info(
        "AI QA Agent HTTP ready; Slack Socket Mode is connecting in the background"
    )
    yield
    slack_task.cancel()
    with suppress(asyncio.CancelledError):
        await slack_task
    await stop_socket_mode()
    await processor.shutdown()
    logger.info("AI QA Agent shutdown complete")


app = FastAPI(
    title="AI QA Agent",
    description="Slack → Ollama → Jira pipeline for QA issue intake",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe; includes whether Slack Socket Mode is connected."""
    return {
        "status": "ok",
        "service": "ai-qa-agent",
        "slack_socket_connected": slack_pkg.slack_handler.slack_socket_connected,
    }
