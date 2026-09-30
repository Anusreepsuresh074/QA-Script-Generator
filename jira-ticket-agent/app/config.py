"""Application configuration loaded from environment variables."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PATH = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """
    Typed settings for secrets and operational tuning.
    Uses a .env file when present (python-dotenv via pydantic-settings).
    """

    model_config = SettingsConfigDict(
        env_file=str(_DOTENV_PATH) if _DOTENV_PATH.is_file() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Ollama (OpenAI-compatible chat completions, local)
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        alias="OLLAMA_BASE_URL",
    )
    ollama_model: str = Field(
        default="llama3.2",
        alias="OLLAMA_MODEL",
    )
    ollama_api_key: str = Field(..., alias="OLLAMA_API_KEY")

    # Slack
    slack_bot_token: str = Field(..., alias="SLACK_BOT_TOKEN")
    slack_app_token: str = Field(..., alias="SLACK_APP_TOKEN")
    slack_channel_ids: str = Field(default="", alias="SLACK_CHANNEL_IDS")

    # Jira
    jira_email: str = Field(..., alias="JIRA_EMAIL")
    jira_api_token: str = Field(..., alias="JIRA_API_TOKEN")
    jira_base_url: str = Field(..., alias="JIRA_BASE_URL")
    jira_project_key: str = Field(default="QA", alias="JIRA_PROJECT_KEY")
    jira_issue_type: str = Field(default="Bug", alias="JIRA_ISSUE_TYPE")
    jira_default_priority: str = Field(default="Medium", alias="JIRA_DEFAULT_PRIORITY")
    jira_module_assignee_map_raw: str = Field(default="{}", alias="JIRA_MODULE_ASSIGNEE_MAP")

    jira_request_timeout: int = Field(default=30, alias="JIRA_REQUEST_TIMEOUT")

    # Duplicate cache
    duplicate_cache_ttl_seconds: int = Field(
        default=86400, alias="DUPLICATE_CACHE_TTL_SECONDS"
    )
    duplicate_cache_max_entries: int = Field(
        default=500, alias="DUPLICATE_CACHE_MAX_ENTRIES"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("ollama_base_url", mode="before")
    @classmethod
    def strip_ollama_base_url(cls, v: Any) -> str:
        if v is None:
            return "http://localhost:11434"
        s = str(v).strip().strip('"').strip("'").rstrip("/")
        return s or "http://localhost:11434"

    @field_validator("ollama_model", mode="before")
    @classmethod
    def strip_ollama_model(cls, v: Any) -> str:
        if v is None:
            return "llama3.2"
        s = str(v).strip().strip('"').strip("'")
        return s or "llama3.2"

    @field_validator("ollama_api_key", mode="before")
    @classmethod
    def strip_ollama_api_key(cls, v: Any) -> str:
        if v is None:
            raise ValueError("OLLAMA_API_KEY is required")
        s = str(v).strip().strip('"').strip("'")
        if not s:
            raise ValueError("OLLAMA_API_KEY is required")
        return s

    @field_validator("slack_channel_ids", mode="before")
    @classmethod
    def strip_slack_channel_ids(cls, v: Any) -> str:
        """Strip wrapping quotes from .env values (e.g. SLACK_CHANNEL_IDS=\"C0123\")."""
        if v is None:
            return ""
        s = str(v).strip().strip('"').strip("'")
        return s

    @field_validator("jira_base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def allowed_slack_channel_ids(self) -> set[str]:
        """Empty set means: do not filter by channel (all channels bot can see)."""
        parts: list[str] = []
        for c in self.slack_channel_ids.split(","):
            x = c.strip().strip('"').strip("'")
            if x:
                parts.append(x)
        return set(parts)

    @property
    def jira_module_assignee_map(self) -> dict[str, str]:
        """Maps module/platform keyword (lowercase) to assignee email."""
        try:
            raw: Any = json.loads(self.jira_module_assignee_map_raw or "{}")
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k).lower(): str(v) for k, v in raw.items()}


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton suitable for import-time wiring."""
    return Settings()  # type: ignore[call-arg]
