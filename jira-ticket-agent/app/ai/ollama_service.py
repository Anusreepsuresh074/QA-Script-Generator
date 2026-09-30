"""Ollama (OpenAI-compatible) client: JSON-mode chat + Pydantic validation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.models import GeminiQAAnalysis, SlackMessageContext
from app.prompts import (
    SYSTEM_INSTRUCTION,
    build_retry_prompt,
    build_user_prompt,
)

logger = logging.getLogger(__name__)

_FENCE_PATTERN = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def extract_json_object(raw: str) -> dict[str, Any]:
    """
    Parse model output that should be JSON. Strips optional markdown fences
    and grabs the first top-level JSON object if extra text is present.
    """
    text = raw.strip()
    m = _FENCE_PATTERN.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("No valid JSON object found in model output")


class OllamaTransientError(Exception):
    """HTTP conditions worth retrying (upstream errors, connection issues)."""


class QAAnalysisError(Exception):
    """Raised when Ollama output cannot be coerced into GeminiQAAnalysis."""


def slack_text_for_llm_failure(exc: QAAnalysisError) -> str:
    """Short Slack hint; full detail stays in logs."""
    s = str(exc).lower()
    if "connection" in s or "refused" in s:
        return (
            ":warning: **Ollama** is not reachable. "
            "Start it with `ollama serve` and ensure `OLLAMA_BASE_URL` in `.env` is correct."
        )
    if "401" in s or "unauthorized" in s or "api key" in s:
        return (
            ":warning: **Ollama** rejected the API key (`OLLAMA_API_KEY`). "
            "Check the key in `.env` and restart the app."
        )
    if "404" in s or (
        "model" in s
        and ("not found" in s or "does not exist" in s or "unknown model" in s)
    ):
        return (
            ":warning: **Ollama** model name is invalid or not pulled. "
            "Set `OLLAMA_MODEL` in `.env` and run `ollama pull <model>`."
        )
    return (
        ":warning: QA Agent could not analyze this message (AI error). "
        "Check **server logs** for the technical reason."
    )


class OllamaService:
    """Chat completions against Ollama's OpenAI-compatible API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _chat_url(self) -> str:
        base = self._settings.ollama_base_url.rstrip("/")
        if base.endswith("/v1/chat/completions"):
            return base
        return f"{base}/v1/chat/completions"

    def _is_local_ollama(self) -> bool:
        host = self._settings.ollama_base_url.lower()
        return "localhost" in host or "127.0.0.1" in host

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.ollama_api_key and not self._is_local_ollama():
            headers["Authorization"] = f"Bearer {self._settings.ollama_api_key}"
        return headers

    def _system_content(self) -> str:
        return (
            SYSTEM_INSTRUCTION
            + "\n\nYou must reply with a single JSON object only (no markdown fences). "
            "The object must match these keys and types: "
            "is_actionable_qa_issue (boolean), issue_category (string, one of: bug, enhancement, "
            "regression, ui_issue, crash, performance, not_qa_relevant), short_summary (string), "
            "jira_summary (string), environment (string), steps_to_reproduce (array of strings), "
            "expected_result (string), actual_result (string), impact (string), priority (string), "
            "severity (string), module (string), platform (string), suggested_labels (array of strings), "
            "duplicate_hint (string), reasoning_notes (string), assignee_hint (string), sprint_name (string), "
            "reporter_hint (string), jira_issue_type_override (string)."
        )

    def _generate_sync(self, user_prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": self._settings.ollama_model,
            "temperature": 0.2,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._system_content()},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = self._auth_headers()
        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    self._chat_url(),
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException:
            raise
        except httpx.RequestError as exc:
            raise OllamaTransientError(str(exc)) from exc
        if resp.status_code in (429, 500, 502, 503):
            raise OllamaTransientError(f"Ollama HTTP {resp.status_code}: {resp.text[:500]}")
        if resp.status_code == 401:
            raise QAAnalysisError(f"Ollama API 401 Unauthorized: {resp.text[:300]}")
        if resp.status_code == 404:
            raise QAAnalysisError(
                f"Ollama model '{self._settings.ollama_model}' not found — "
                f"run: ollama pull {self._settings.ollama_model}"
            )
        if resp.status_code >= 400:
            raise QAAnalysisError(f"Ollama API error {resp.status_code}: {resp.text[:800]}")

        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            raise QAAnalysisError(f"Ollama returned non-JSON body: {resp.text[:500]}") from exc
        choices = body.get("choices") or []
        if not choices:
            raise QAAnalysisError(f"Empty Ollama choices: {body!r}")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise QAAnalysisError("Empty Ollama response content")
        return content

    @retry(
        retry=retry_if_exception_type((OllamaTransientError, httpx.TimeoutException)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _generate_with_retry(self, user_prompt: str) -> str:
        return self._generate_sync(user_prompt)

    async def analyze_slack_message(self, ctx: SlackMessageContext) -> GeminiQAAnalysis:
        """
        Run Ollama in a worker thread and validate with Pydantic.
        On validation failure, one repair attempt with explicit error feedback.
        """
        primary = build_user_prompt(ctx)

        def _run(first: str) -> GeminiQAAnalysis:
            raw = self._generate_with_retry(first)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = extract_json_object(raw)
            return GeminiQAAnalysis.model_validate(data)

        try:
            return await asyncio.to_thread(_run, primary)
        except QAAnalysisError:
            raise
        except OllamaTransientError as exc:
            logger.warning("Ollama failed after retries (no repair): %s", exc)
            raise QAAnalysisError(str(exc)) from exc
        except Exception as first_exc:
            logger.warning("Ollama first pass failed: %s", first_exc)
            repair = build_retry_prompt(ctx, str(first_exc))

            def _repair() -> GeminiQAAnalysis:
                raw = self._generate_with_retry(repair)
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = extract_json_object(raw)
                return GeminiQAAnalysis.model_validate(data)

            try:
                return await asyncio.to_thread(_repair)
            except Exception as second_exc:
                raise QAAnalysisError(
                    f"QA analysis failed after repair: {second_exc}"
                ) from second_exc
