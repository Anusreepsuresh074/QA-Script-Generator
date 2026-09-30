"""Shared Ollama chat client (OpenAI-compatible API) with retry on transient errors."""

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


class OllamaTransientError(Exception):
    """Connection and upstream errors that should be retried."""


class OllamaClient:
    """Thin Ollama wrapper using the OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    def _chat_url(self) -> str:
        base = self._settings.ollama_base_url.rstrip("/")
        if base.endswith("/v1/chat/completions"):
            return base
        return f"{base}/v1/chat/completions"

    def _is_local_ollama(self) -> bool:
        host = self._settings.ollama_base_url.lower()
        return "localhost" in host or "127.0.0.1" in host

    def chat_completion(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 8192,
        response_format: Optional[dict[str, str]] = None,
        timeout: int = 180,
        max_attempts: int = 4,
    ) -> str:
        """Call Ollama chat completions with automatic retries."""

        @retry(
            retry=retry_if_exception_type((OllamaTransientError, requests.Timeout)),
            wait=wait_exponential(multiplier=2, min=5, max=60),
            stop=stop_after_attempt(max_attempts),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        def _call() -> str:
            effective_max = max_tokens
            if "gpt-oss" in self._settings.ollama_model.lower():
                # gpt-oss models spend tokens on internal reasoning before content
                effective_max = max(max_tokens, 512)

            payload: dict[str, Any] = {
                "model": self._settings.ollama_model,
                "temperature": temperature,
                "max_tokens": effective_max,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if response_format:
                payload["response_format"] = response_format

            headers = {"Content-Type": "application/json"}
            # Local Ollama ignores Bearer tokens and may hang; cloud requires the key.
            if self._settings.ollama_api_key and not self._is_local_ollama():
                headers["Authorization"] = f"Bearer {self._settings.ollama_api_key}"

            try:
                resp = requests.post(
                    self._chat_url(),
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                )
            except requests.ConnectionError as exc:
                raise OllamaTransientError(
                    f"Ollama connection error — is Ollama running at {self._settings.ollama_base_url}? {exc}"
                ) from exc
            except requests.Timeout as exc:
                raise OllamaTransientError(f"Ollama request timed out: {exc}") from exc
            except requests.RequestException as exc:
                raise OllamaTransientError(f"Ollama request failed: {exc}") from exc

            if resp.status_code in (429, 500, 502, 503):
                raise OllamaTransientError(
                    f"Ollama HTTP {resp.status_code}: {resp.text[:200]}"
                )

            if resp.status_code == 401:
                raise ValueError("Ollama API key is invalid — check OLLAMA_API_KEY in .env")

            if resp.status_code == 404:
                raise ValueError(
                    f"Ollama model '{self._settings.ollama_model}' not found — "
                    f"run: ollama pull {self._settings.ollama_model}"
                )

            if resp.status_code >= 400:
                raise ValueError(f"Ollama error {resp.status_code}: {resp.text[:400]}")

            body = resp.json()
            choices = body.get("choices") or []
            if not choices:
                raise ValueError("Empty response from Ollama")

            content = (choices[0].get("message") or {}).get("content", "").strip()
            if not content:
                raise ValueError("Empty content from Ollama — the model may have timed out or hit token limits")
            return content

        return _call()
