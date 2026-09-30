"""
Centralized application configuration loaded from environment variables.
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class MappingType(str, Enum):
    KEYWORD = "keyword"
    EMBEDDING = "embedding"
    OLLAMA = "ollama"
    GROQ = "groq"  # legacy alias — same LLM path as ollama


class Settings(BaseSettings):
    """Application-wide settings sourced from `.env` or OS environment."""

    # Jira
    jira_url: str = Field(..., description="Base URL of the Jira instance")
    jira_email: str = Field(..., description="Jira account email")
    jira_api_token: str = Field(..., description="Jira API token")
    jira_project_key: str = Field("QA", description="Default Jira project key for ticket listing")
    jira_request_timeout: int = Field(30, description="HTTP timeout for Jira calls (seconds)")

    # Ollama LLM (primary)
    ollama_base_url: str = Field(
        "https://ollama.com",
        description="Ollama server URL (cloud: https://ollama.com, local: http://localhost:11434)",
    )
    ollama_model: str = Field("gpt-oss:120b", description="Ollama model ID")
    ollama_api_key: str = Field(..., description="Ollama API key (required for cloud)")

    # Groq LLM (fallback)
    groq_api_key: str = Field(..., description="Groq Cloud API key (fallback)")
    groq_model: str = Field("llama-3.3-70b-versatile", description="Groq model ID (fallback)")

    # Swagger / OpenAPI
    swagger_request_timeout: int = Field(30, description="HTTP timeout for Swagger fetches (seconds)")
    swagger_url: Optional[str] = Field(
        None,
        description="Default Swagger/OpenAPI spec URL used when a request doesn't supply one",
    )

    # Mapping
    mapping_type: MappingType = Field(
        MappingType.KEYWORD,
        description="Requirement-to-API mapping strategy",
    )
    embedding_model: str = Field(
        "all-MiniLM-L6-v2",
        description="Sentence-transformer model for semantic mapping",
    )
    top_p: float = Field(
        0.5,
        description="Minimum relevance score (0–1) for an endpoint to be included",
    )

    # Phase 2 – Scenario budget
    max_scenarios_per_criterion: int = Field(
        3,
        description="Hard cap on scenarios generated per acceptance criterion",
    )
    max_total_scenarios: int = Field(
        20,
        description="Hard cap on total scenarios per ticket (0 = no cap)",
    )

    # Logging
    log_level: str = Field("INFO", description="Root log level")

    # FastAPI
    app_host: str = Field("0.0.0.0", description="FastAPI bind host")
    app_port: int = Field(8000, description="FastAPI bind port")

    # Phase 3 – Pytest script output
    output_dir: str = Field("output", description="Directory for generated test files")
    reports_dir: str = Field(
        "reports",
        description="Directory for test-run reports (latest + archived history)",
    )
    api_base_url: str = Field(
        "https://reqres.in/api",
        description="Default base URL injected into generated conftest.py",
    )
    api_key_default: str = Field(
        "",
        description="Default API key for generated tests (override via API_KEY env at runtime)",
    )

    # GitHub – automation PRs for generated tests (auto after a run, or on demand)
    github_token: str = Field(
        "",
        description="GitHub token for PR creation (optional if `gh` CLI is authenticated)",
    )
    github_automation_repo: str = Field(
        "",
        description="Automation repo as owner/name (e.g. owner/automation-repo)",
    )
    github_pr_base_branch: str = Field(
        "main",
        description="Base branch for automation PRs",
    )
    auto_create_pr: bool = Field(
        False,
        description=(
            "Default for the dashboard 'Create PR automatically after test run' "
            "toggle; when off, PRs are raised on demand via the Raise PR action"
        ),
    )
    github_tests_path: str = Field(
        "tests",
        description=(
            "Fallback directory in the automation repo for generated tests, used "
            "when repo analysis is off and the UI supplies no path"
        ),
    )
    github_analyze_repo: bool = Field(
        True,
        description=(
            "Default for the dashboard 'Analyze target repo' toggle — read the "
            "automation repo's layout, fixtures, and pytest config before a PR "
            "so files land where its own tests live"
        ),
    )
    github_adapt_tests: bool = Field(
        False,
        description=(
            "Default for the dashboard 'Adapt tests to repo conventions' toggle "
            "— rewrite generated modules via Groq to reuse the repo's existing "
            "fixtures and helpers (requires repo analysis)"
        ),
    )
    github_pr_branch_prefix: str = Field(
        "automation",
        description="Prefix for auto-generated PR branch names (automation/<TICKET>)",
    )

    @field_validator("ollama_api_key")
    @classmethod
    def _validate_ollama_api_key(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("OLLAMA_API_KEY is required")
        return v

    @field_validator("groq_api_key")
    @classmethod
    def _validate_groq_api_key(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("GROQ_API_KEY is required")
        return v

    @field_validator("max_scenarios_per_criterion")
    @classmethod
    def _validate_per_criterion_cap(cls, v: int) -> int:
        if v < 1:
            raise ValueError("MAX_SCENARIOS_PER_CRITERION must be >= 1")
        return v

    @field_validator("max_total_scenarios")
    @classmethod
    def _validate_total_cap(cls, v: int) -> int:
        if v < 0:
            raise ValueError("MAX_TOTAL_SCENARIOS must be >= 0 (0 disables the cap)")
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v

    model_config = {
        "env_file": str(_ENV_FILE),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """Return a cached singleton of application settings."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance


def reload_settings() -> Settings:
    """Force-reload settings from `.env` / environment (e.g. after a config change)."""
    global _settings_instance
    _settings_instance = Settings()
    return _settings_instance
