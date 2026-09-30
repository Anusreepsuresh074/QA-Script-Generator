"""Slack Bolt AsyncApp wired for Socket Mode; routes channel messages to BugProcessor."""

from __future__ import annotations

import logging
from typing import Optional

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from app.config import Settings, get_settings
from app.services.bug_processor import BugProcessor

logger = logging.getLogger(__name__)

_socket_handler: Optional[AsyncSocketModeHandler] = None
_bot_user_id: Optional[str] = None
slack_socket_connected: bool = False


def _should_ignore_message_event(event: dict, bot_user_id: str) -> bool:
    """
    Filter out bot noise, empty text, and non-user subtypes.
    Casual / non-QA triage happens later in the LLM.
    """
    if event.get("bot_id"):
        return True
    subtype = event.get("subtype")
    if subtype in (
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
    ):
        return True
    if subtype == "file_share":
        # Allow uploads when there is human-readable text (common for bug + screenshot).
        return not (event.get("text") or "").strip()
    if subtype:
        # Unknown subtype: stay conservative (files, invites, etc.)
        return True
    if event.get("user") == bot_user_id:
        return True
    text = (event.get("text") or "").strip()
    if not text:
        return True
    return False


def create_slack_app(settings: Settings, processor: BugProcessor) -> AsyncApp:
    """
    Build the Bolt app and register the `message` listener.
    Channel allow-list uses SLACK_CHANNEL_IDS; empty means all channels the bot is in.
    """
    app = AsyncApp(token=settings.slack_bot_token)

    @app.event("app_mention")
    async def on_app_mention(
        event: dict,
        client,
        ack,
        logger: logging.Logger,
    ) -> None:
        await ack()

        allowed = settings.allowed_slack_channel_ids
        channel_id = str(event.get("channel") or "")
        if allowed and channel_id not in allowed:
            logger.info(
                "QA agent skipped: channel %s not in SLACK_CHANNEL_IDS allow-list (%s entries)",
                channel_id or "(missing)",
                len(allowed),
            )
            return

        global _bot_user_id
        if _bot_user_id is None:
            auth = await client.auth_test()
            _bot_user_id = str(auth["user_id"])

        if _should_ignore_message_event(event, _bot_user_id):
            return

        logger.info("QA agent handling app_mention channel=%s", channel_id)
        try:
            await processor.handle_slack_message(
                event,
                client,
                bot_user_id=str(_bot_user_id),
            )
        except Exception:
            logger.exception("Unhandled error in message pipeline")

    @app.event("message")
    async def on_message(
        event: dict,
        client,
        ack,
        logger: logging.Logger,
    ) -> None:
        await ack()

        # Ignore bot messages and non-user subtypes.
        if event.get("bot_id") or event.get("subtype"):
            return

        text = (event.get("text") or "").strip()
        if not text:
            return

        global _bot_user_id
        if _bot_user_id is None:
            auth = await client.auth_test()
            _bot_user_id = str(auth["user_id"])

        if event.get("user") == _bot_user_id:
            return

        channel_id = str(event.get("channel") or "")
        allowed = settings.allowed_slack_channel_ids
        if allowed and channel_id not in allowed:
            logger.info(
                "QA agent skipped (message): channel %s not in allow-list", channel_id
            )
            return

        has_mention = f"<@{_bot_user_id}>" in text
        thread_ts = event.get("thread_ts")

        # Route if: direct bot mention (app_mention fallback)
        # OR a thread reply in a thread where the intake form was already posted.
        should_route = has_mention
        if not should_route and thread_ts:
            from app.services.bug_processor import _intake_form_in_thread
            should_route = await _intake_form_in_thread(client, channel_id, str(thread_ts))

        if not should_route:
            return

        logger.info(
            "QA agent handling message event (mention=%s thread_reply=%s) channel=%s",
            has_mention,
            bool(thread_ts and not has_mention),
            channel_id,
        )
        try:
            await processor.handle_slack_message(
                event,
                client,
                bot_user_id=_bot_user_id,
            )
        except Exception:
            logger.exception("Unhandled error in message pipeline (message fallback)")

    @app.error
    async def on_error(error, body, logger: logging.Logger) -> None:
        logger.error("Slack Bolt error: %s | body: %s", error, str(body)[:500])

    return app


async def start_socket_mode_processor(processor: BugProcessor) -> AsyncSocketModeHandler:
    """Connect Slack Socket Mode (WebSocket) and return the handler for shutdown."""
    global _socket_handler, slack_socket_connected
    settings = get_settings()
    slack_app = create_slack_app(settings, processor)
    _socket_handler = AsyncSocketModeHandler(
        slack_app,
        app_token=settings.slack_app_token,
    )
    await _socket_handler.connect_async()
    slack_socket_connected = True
    logger.info("Slack Socket Mode connected for AI QA Agent")
    return _socket_handler


async def stop_socket_mode() -> None:
    """Cleanly close the Slack WebSocket client."""
    global _socket_handler, slack_socket_connected
    if _socket_handler is not None:
        await _socket_handler.close_async()
        _socket_handler = None
        slack_socket_connected = False
        logger.info("Slack Socket Mode disconnected")
