"""Orchestrates Slack context → Ollama LLM → dedupe → Jira → Slack acknowledgement."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import httpx
from slack_sdk.web.async_client import AsyncWebClient

from app.ai.ollama_service import (
    OllamaService,
    QAAnalysisError,
    slack_text_for_llm_failure,
)
from app.config import Settings
from app.jira.jira_service import JiraService, JiraServiceError
from app.models import GeminiQAAnalysis, IssueCategory, SlackMessageContext
from app.prompts import INTAKE_FORM_HEADER, build_intake_form

logger = logging.getLogger(__name__)

_SLACK_USER_MENTION = re.compile(r"<@([A-Z0-9]+)>")
_INTAKE_TYPE_RE = re.compile(r"^\s*Type\s*:\s*(Bug|Task|Story)\b", re.IGNORECASE | re.MULTILINE)
_INTAKE_REQ_RE = re.compile(r"^\s*(?:Requirement|Content)\s*:\s*(.+)", re.IGNORECASE | re.MULTILINE)
_TYPE_TO_CATEGORY: dict[str, str] = {
    "bug": "bug",
    "task": "enhancement",
    "story": "enhancement",
}


def _has_filled_intake_form(text: str) -> bool:
    """True when the text contains a Type: Bug|Task|Story line and a non-empty Requirement/Content line."""
    if not _INTAKE_TYPE_RE.search(text):
        return False
    m = _INTAKE_REQ_RE.search(text)
    return bool(m and m.group(1).strip())

def _strip_bot_mentions(text: str, bot_user_id: str | None) -> str:
    if not (bot_user_id or "").strip():
        return text
    bid = bot_user_id.strip()
    return re.sub(
        rf"<@{re.escape(bid)}(\|[^>]*)?>\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


async def _intake_form_in_thread(
    client: AsyncWebClient,
    channel_id: str,
    thread_ts: str,
) -> bool:
    """Returns True if the bot's intake form was already posted in this thread."""
    try:
        result = await client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=20,
        )
        return any(
            INTAKE_FORM_HEADER in (msg.get("text") or "")
            for msg in result.get("messages", [])
        )
    except Exception as exc:
        logger.warning("Thread intake form check failed: %s", exc)
        return False


async def _slack_reporter_display_name(client: AsyncWebClient, user_id: str | None) -> str:
    if not user_id:
        return ""
    try:
        res = await client.users_info(user=user_id)
        u = res.get("user") or {}
        prof = u.get("profile") or {}
        return (
            (u.get("real_name") or "").strip()
            or (prof.get("real_name") or "").strip()
            or (u.get("name") or "").strip()
        )
    except Exception as exc:
        logger.debug("users_info for reporter failed: %s", exc)
        return ""


async def _expand_slack_user_mentions(
    client: AsyncWebClient,
    text: str,
    cache: dict[str, str],
) -> str:
    """Replace <@U123> with @Display Name so the LLM can put a real name in assignee_hint."""
    if not text or "<@" not in text:
        return text
    for uid in set(_SLACK_USER_MENTION.findall(text)):
        if uid in cache:
            continue
        try:
            res = await client.users_info(user=uid)
            u = res.get("user") or {}
            prof = u.get("profile") or {}
            disp = (
                (u.get("real_name") or "").strip()
                or (prof.get("real_name") or "").strip()
                or (u.get("name") or "").strip()
                or uid
            )
            cache[uid] = disp
        except Exception as exc:
            logger.debug("users_info failed for %s: %s", uid, exc)
            cache[uid] = uid

    def _repl(m: re.Match[str]) -> str:
        return f"@{cache.get(m.group(1), m.group(1))}"

    return _SLACK_USER_MENTION.sub(_repl, text)


def _jira_http_error_hint(exc: BaseException) -> str:
    """Short text from Jira REST JSON error for Slack (no stack traces)."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response is None:
        return ""
    try:
        data = exc.response.json()
    except Exception:
        return (exc.response.text or "").strip()[:400]
    msgs = data.get("errorMessages") or []
    errs = data.get("errors") or {}
    parts: list[str] = [str(m) for m in msgs if m]
    for k, v in errs.items():
        if isinstance(v, str):
            parts.append(f"{k}: {v}")
    if not parts:
        return ""
    return " — ".join(parts)[:450]


def _fingerprint_for_issue(analysis: GeminiQAAnalysis) -> str:
    """Stable hash for duplicate detection across similar phrasing."""
    raw = "|".join(
        [
            (analysis.duplicate_hint or "").lower().strip(),
            analysis.jira_summary.lower().strip(),
            analysis.issue_category,
            analysis.module.lower().strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DuplicateTracker:
    """In-memory TTL cache of recent issue fingerprints → Jira keys."""

    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        expired = [k for k, (ts, _) in self._data.items() if now - ts > self._ttl]
        for k in expired:
            del self._data[k]
        while len(self._data) > self._max:
            oldest_key = min(self._data.items(), key=lambda x: x[1][0])[0]
            del self._data[oldest_key]

    async def get_existing_jira_key(self, fingerprint: str) -> str | None:
        async with self._lock:
            now = time.time()
            self._prune(now)
            entry = self._data.get(fingerprint)
            if not entry:
                return None
            ts, key = entry
            if now - ts > self._ttl:
                del self._data[fingerprint]
                return None
            return key

    async def remember(self, fingerprint: str, jira_key: str) -> None:
        async with self._lock:
            self._data[fingerprint] = (time.time(), jira_key)
            self._prune(time.time())


class BugProcessor:
    """
        Core workflow: build SlackMessageContext, call Ollama, skip casual content,
    dedupe, create Jira, reply in-thread on Slack.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm = OllamaService(settings)
        self._jira = JiraService(settings)
        self._dupes = DuplicateTracker(
            settings.duplicate_cache_ttl_seconds,
            settings.duplicate_cache_max_entries,
        )

    async def shutdown(self) -> None:
        await self._jira.aclose()

    async def _fetch_thread_lines(
        self,
        client: AsyncWebClient,
        channel_id: str,
        thread_root_ts: str,
    ) -> list[str]:
        """Pull a bounded thread transcript for richer LLM context."""
        lines: list[str] = []
        try:
            result = await client.conversations_replies(
                channel=channel_id,
                ts=thread_root_ts,
                limit=30,
            )
            for msg in result.get("messages", []):
                uid = msg.get("user") or msg.get("username") or "unknown"
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                lines.append(f"<@{uid}>: {text}")
        except Exception as exc:
            logger.warning("Thread fetch failed: %s", exc)
        return lines

    async def build_slack_context(
        self,
        client: AsyncWebClient,
        event: dict,
    ) -> SlackMessageContext:
        """Normalize Bolt message event + optional thread history."""
        channel_id = str(event["channel"])
        message_ts = str(event["ts"])
        thread_ts = event.get("thread_ts")
        if thread_ts:
            thread_ts = str(thread_ts)
        user_id = event.get("user") or event.get("user_id")
        text = (event.get("text") or "").strip()

        thread_messages: list[str] = []
        root_ts = thread_ts or message_ts
        if thread_ts is not None:
            thread_messages = await self._fetch_thread_lines(client, channel_id, root_ts)
        elif int(event.get("reply_count", 0) or 0) > 0:
            thread_messages = await self._fetch_thread_lines(client, channel_id, message_ts)

        mention_cache: dict[str, str] = {}
        text = await _expand_slack_user_mentions(client, text, mention_cache)
        thread_messages = [
            await _expand_slack_user_mentions(client, line, mention_cache)
            for line in thread_messages
        ]

        reporter = await _slack_reporter_display_name(client, str(user_id) if user_id else None)

        return SlackMessageContext(
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=thread_ts,
            user_id=str(user_id) if user_id else None,
            text=text,
            thread_messages=thread_messages,
            slack_reporter_display_name=reporter,
        )

    def _reply_thread_ts(self, event: dict) -> str:
        """Slack thread_ts for replies: use parent ts when in thread else start thread from root."""
        return str(event.get("thread_ts") or event["ts"])

    async def _post_slack(
        self,
        client: AsyncWebClient,
        channel_id: str,
        thread_ts: str,
        text: str,
    ) -> None:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )

    async def handle_slack_message(
        self,
        event: dict,
        client: AsyncWebClient,
        bot_user_id: str | None = None,
    ) -> None:
        """
        Two-step flow:
          Step 1 — bot is mentioned in a channel (no intake form in thread yet):
                   reply with the intake form template.
          Step 2 — bot is mentioned again in the same thread after the form was posted:
                   parse the filled form, call LLM, create Jira ticket.
        """
        channel_id = str(event["channel"])
        thread_ts = self._reply_thread_ts(event)
        raw_text = (event.get("text") or "").strip()

        if not raw_text:
            return

        # --- Step 1: first mention (no thread, or thread without our form) ---
        event_thread_ts = event.get("thread_ts")
        form_already_sent = (
            await _intake_form_in_thread(client, channel_id, str(event_thread_ts))
            if event_thread_ts
            else False
        )

        if not form_already_sent:
            await self._post_slack(
                client,
                channel_id,
                thread_ts,
                build_intake_form(bot_user_id or ""),
            )
            return

        # --- Step 2: user replied with filled form ---
        try:
            ctx = await self.build_slack_context(client, event)
        except Exception as exc:
            logger.exception("Context build failed: %s", exc)
            return

        if not ctx.text:
            return

        ctx = ctx.model_copy(update={"text": _strip_bot_mentions(ctx.text, bot_user_id)})
        if not ctx.text.strip():
            await self._post_slack(
                client,
                channel_id,
                thread_ts,
                ":thinking_face: Please fill in the form details above before submitting.",
            )
            return

        try:
            analysis = await self._llm.analyze_slack_message(ctx)
        except QAAnalysisError as exc:
            logger.error("LLM analysis failed: %s", exc)
            await self._post_slack(
                client,
                channel_id,
                thread_ts,
                slack_text_for_llm_failure(exc),
            )
            return
        except Exception as exc:
            logger.exception("Unexpected LLM error: %s", exc)
            return

        if not analysis.is_actionable_qa_issue:
            # Override if the LLM extracted a structured Type: or the raw text has the intake form.
            override_type = analysis.jira_issue_type_override.strip().lower()
            if override_type or _has_filled_intake_form(ctx.text):
                fallback_category = _TYPE_TO_CATEGORY.get(override_type, "enhancement")
                analysis = analysis.model_copy(update={
                    "is_actionable_qa_issue": True,
                    "issue_category": fallback_category,
                })
                logger.info("Intake form detected — overriding LLM triage to actionable (type=%r)", override_type)
            else:
                logger.info(
                    "LLM triage: not filing Jira (actionable=%s category=%s summary=%r)",
                    analysis.is_actionable_qa_issue,
                    analysis.issue_category,
                    (analysis.jira_summary or "")[:120],
                )
                await self._post_slack(
                    client,
                    channel_id,
                    thread_ts,
                    ":thinking_face: I couldn't identify a clear issue from the details provided. "
                    "Please make sure the *Requirement* field describes the bug, task, or story clearly.",
                )
                return

        # Normalize contradictory not_qa_relevant category when LLM marks issue as actionable.
        if analysis.issue_category == IssueCategory.NOT_QA_RELEVANT.value:
            override_type = analysis.jira_issue_type_override.strip().lower()
            analysis = analysis.model_copy(update={
                "issue_category": _TYPE_TO_CATEGORY.get(override_type, "enhancement"),
            })

        rep_slack = (ctx.slack_reporter_display_name or "").strip()
        if rep_slack and not (analysis.reporter_hint or "").strip():
            analysis = analysis.model_copy(update={"reporter_hint": rep_slack})

        fp = _fingerprint_for_issue(analysis)
        existing = await self._dupes.get_existing_jira_key(fp)
        if existing:
            await self._post_slack(
                client,
                channel_id,
                thread_ts,
                (
                    f":repeat: This looks like a **duplicate** of a recently filed issue: "
                    f"*{existing}*\n_No new Jira ticket was created._"
                ),
            )
            return

        try:
            created = await self._jira.create_issue_from_analysis(analysis)
        except JiraServiceError as exc:
            logger.error("Jira logical error: %s", exc)
            await self._post_slack(
                client,
                channel_id,
                thread_ts,
                f":warning: Jira ticket could not be created: `{exc}`",
            )
            return
        except Exception as exc:
            logger.exception("Jira API failure: %s", exc)
            hint = _jira_http_error_hint(exc)
            extra = ""
            if hint and "valid project" in hint.lower():
                extra = (
                    " Update **`JIRA_PROJECT_KEY`** in `.env` to a project key you can access "
                    "(see Jira URL `/browse/YOURKEY` or Project settings)."
                )
            await self._post_slack(
                client,
                channel_id,
                thread_ts,
                (
                    ":warning: Jira ticket creation failed."
                    + (f" {hint}" if hint else " Check server logs.")
                    + extra
                ),
            )
            return

        await self._dupes.remember(fp, created.key)

        assignee_line = (
            created.assignee_display_name
            or (f"accountId `{created.assignee_account_id}`" if created.assignee_account_id else "Unassigned")
        )
        ticket_url = f"{self._settings.jira_base_url}/browse/{created.key}"
        ack = (
            f":white_check_mark: *Jira created:* <{ticket_url}|{created.key}>\n"
            f"*Summary:* {analysis.jira_summary}\n"
            f"*Priority:* {analysis.priority}  |  *Severity:* {analysis.severity}\n"
            f"*Assignee:* {assignee_line}\n"
            + (f"*Reporter:* {created.reporter_display_name}\n" if created.reporter_display_name else "")
            + (f"*Sprint:* {created.sprint_matched_name}\n" if created.sprint_matched_name else "")
        )
        await self._post_slack(client, channel_id, thread_ts, ack)
