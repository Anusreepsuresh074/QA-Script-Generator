"""LLM client: Ollama first, Groq fallback on failure or timeout."""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.ai.groq_client import GroqClient, GroqTransientError
from app.ai.ollama_client import OllamaClient, OllamaTransientError
from app.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class LLMTransientError(Exception):
    """Both Ollama and Groq failed after retries."""


class LLMClient:
    """Try Ollama (primary); fall back to Groq on transient errors or bad responses."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._ollama = OllamaClient(self._settings)
        self._groq = GroqClient(self._settings)
        self.last_provider: str = ""

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
        ollama_error: Exception | None = None
        try:
            result = self._ollama.chat_completion(
                system=system,
                user=user,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                timeout=timeout,
                max_attempts=2,
            )
            self.last_provider = "ollama"
            return result
        except (OllamaTransientError, ValueError) as exc:
            ollama_error = exc
            logger.warning("Ollama unavailable (%s) — falling back to Groq", exc)

        try:
            result = self._groq.chat_completion(
                system=system,
                user=user,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                timeout=timeout,
            )
            self.last_provider = "groq"
            logger.info("Groq fallback succeeded (model=%s)", self._settings.groq_model)
            return result
        except (GroqTransientError, ValueError) as exc:
            raise LLMTransientError(
                f"Ollama failed ({ollama_error}); Groq fallback also failed ({exc})"
            ) from exc
