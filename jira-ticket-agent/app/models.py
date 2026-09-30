"""Pydantic models for AI output validation, Slack context, and Jira payloads."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Inline enum for LLM JSON (Pydantic Enum → $defs + $ref; Ollama uses plain JSON + validation).
IssueCategoryLiteral = Literal[
    "bug",
    "enhancement",
    "regression",
    "ui_issue",
    "crash",
    "performance",
    "not_qa_relevant",
]


class IssueCategory(str, Enum):
    """High-level classification used for labels and routing."""

    BUG = "bug"
    ENHANCEMENT = "enhancement"
    REGRESSION = "regression"
    UI_ISSUE = "ui_issue"
    CRASH = "crash"
    PERFORMANCE = "performance"
    NOT_QA_RELEVANT = "not_qa_relevant"


class GeminiQAAnalysis(BaseModel):
    """
    Structured LLM output (JSON), validated before Jira creation.
    The model acts as senior QA + release manager per prompts.py.
    """

    is_actionable_qa_issue: bool = Field(
        ...,
        description="True only if the message describes a verifiable QA/bug topic.",
    )
    issue_category: IssueCategoryLiteral
    # No Field(max_length=…): keep JSON schema simple for LLM JSON mode.
    short_summary: str = Field(..., description="One-line summary; keep under 500 characters.")
    jira_summary: str = Field(
        ...,
        description="Jira summary line; keep under 255 characters (Jira limit).",
    )
    environment: str = Field(default="", description="OS, app version, region, etc.")
    steps_to_reproduce: list[str] = Field(default_factory=list)
    expected_result: str = Field(default="")
    actual_result: str = Field(default="")
    impact: str = Field(default="")
    priority: str = Field(
        default="Medium",
        description="Suggested Jira priority label: Low, Medium, High, Highest",
    )
    severity: str = Field(
        default="Major",
        description="Business/technical severity e.g. Critical, Major, Minor, Trivial",
    )
    module: str = Field(default="", description="Product area or component name.")
    platform: str = Field(
        default="",
        description="web, ios, android, backend, api, etc.",
    )
    suggested_labels: list[str] = Field(default_factory=list)
    duplicate_hint: str = Field(
        default="",
        description="Normalized fingerprint text to help dedupe; empty if not applicable.",
    )
    reasoning_notes: str = Field(
        default="",
        description="Brief internal rationale (not posted to Jira).",
    )
    assignee_hint: str = Field(
        default="",
        description=(
            "Person to assign: email, display name, or @handle as stated in Slack; "
            "empty if not specified."
        ),
    )
    sprint_name: str = Field(
        default="",
        description="Jira sprint name or phrase from Slack if user asked to add to a sprint; empty otherwise.",
    )
    reporter_hint: str = Field(
        default="",
        description=(
            "Person reporting the issue: from structured 'Reporter:' line in Slack; "
            "empty means use Slack reporter display name supplied in the user prompt."
        ),
    )
    jira_issue_type_override: str = Field(
        default="",
        description=(
            "If the user gave structured 'Type: bug|task|story', set to Bug, Task, or Story "
            "for Jira issuetype name; empty means use project default from server config."
        ),
    )

    @field_validator("assignee_hint", "sprint_name", "reporter_hint", mode="before")
    @classmethod
    def strip_assignee_sprint_hints(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()[:500]

    @field_validator("jira_issue_type_override", mode="before")
    @classmethod
    def cap_issue_type_override(cls, v: Any) -> str:
        s = "" if v is None else str(v).strip()
        return s[:80]

    @field_validator("short_summary", mode="before")
    @classmethod
    def cap_short_summary(cls, v: Any) -> str:
        s = "" if v is None else str(v).strip()
        return s[:500]

    @field_validator("jira_summary", mode="before")
    @classmethod
    def cap_jira_summary(cls, v: Any) -> str:
        s = "" if v is None else str(v).strip()
        return s[:255]

    @field_validator("suggested_labels", mode="before")
    @classmethod
    def strip_labels(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []


class SlackMessageContext(BaseModel):
    """Normalized Slack payload + optional thread transcript for the model."""

    channel_id: str
    message_ts: str
    thread_ts: str | None
    user_id: str | None
    text: str
    thread_messages: list[str] = Field(default_factory=list)
    slack_reporter_display_name: str = Field(
        default="",
        description="Slack profile display name of the user who sent the message.",
    )


class JiraCreatedIssue(BaseModel):
    """Minimal response after creating an issue."""

    key: str
    self_url: str
    assignee_display_name: str | None = None
    assignee_account_id: str | None = None
    reporter_display_name: str | None = None
    sprint_matched_name: str | None = None
