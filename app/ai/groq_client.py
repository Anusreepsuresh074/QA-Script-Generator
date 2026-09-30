"""Shared Groq chat client with automatic retry on rate limits and transient errors."""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqTransientError(Exception):
    """Rate limits and upstream errors that should be retried."""


class GroqClient:
    """Thin Groq wrapper with exponential backoff for 429 / 5xx responses."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    def chat_completion(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 8192,
        response_format: Optional[dict[str, str]] = None,
        timeout: int = 180,
    ) -> str:
        """Call Groq chat completions with automatic retries."""

        @retry(
            retry=retry_if_exception_type((GroqTransientError, requests.Timeout)),
            wait=wait_exponential(multiplier=2, min=15, max=90),
            stop=stop_after_attempt(6),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        def _call() -> str:
            payload: dict[str, Any] = {
                "model": self._settings.groq_model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if response_format:
                payload["response_format"] = response_format

            headers = {
                "Authorization": f"Bearer {self._settings.groq_api_key}",
                "Content-Type": "application/json",
            }
            try:
                resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=timeout)
            except requests.ConnectionError as exc:
                raise GroqTransientError(f"Groq connection error: {exc}") from exc
            except requests.Timeout as exc:
                raise GroqTransientError(f"Groq request timed out: {exc}") from exc
            except requests.RequestException as exc:
                raise GroqTransientError(f"Groq request failed: {exc}") from exc

            if resp.status_code in (429, 500, 502, 503):
                raise GroqTransientError(f"Groq HTTP {resp.status_code}: {resp.text[:200]}")

            if resp.status_code == 401:
                raise ValueError("Groq API key is invalid")

            if resp.status_code >= 400:
                raise ValueError(f"Groq error {resp.status_code}: {resp.text[:400]}")

            body = resp.json()
            choices = body.get("choices") or []
            if not choices:
                raise ValueError("Empty response from Groq")

            content = (choices[0].get("message") or {}).get("content", "").strip()
            if not content:
                raise ValueError("Empty content from Groq")
            return content

        return _call()
