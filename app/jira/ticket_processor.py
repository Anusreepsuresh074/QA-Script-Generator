"""
Transforms raw Jira API responses into ``TicketSchema`` instances.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.jira.criteria_extractor import CriteriaExtractor
from app.models.schemas import TicketSchema
from app.utils.helpers import safe_get, sanitize_text

logger = logging.getLogger(__name__)


class TicketProcessor:
    """Extracts and normalises fields from a raw Jira issue dict."""

    def __init__(self, criteria_extractor: Optional[CriteriaExtractor] = None) -> None:
        self._extractor = criteria_extractor or CriteriaExtractor()

    def extract_ticket_details(self, raw_issue: Dict[str, Any]) -> TicketSchema:
        """Convert the JSON returned by ``GET /issue/{key}`` into a schema."""
        fields: Dict[str, Any] = raw_issue.get("fields", {})
        ticket_id = raw_issue.get("key", "UNKNOWN")

        logger.info("Processing ticket %s", ticket_id)

        description = self._extract_description(fields)
        summary = sanitize_text(fields.get("summary"))
        acceptance_criteria = self._extractor.extract(description, summary=summary)

        return TicketSchema(
            ticket_id=ticket_id,
            summary=summary,
            description=description,
            acceptance_criteria=acceptance_criteria,
            labels=fields.get("labels", []),
            linked_issues=self._extract_linked_issues(fields),
            comments=self._extract_comments(fields),
            priority=safe_get(fields, "priority", "name"),
            status=safe_get(fields, "status", "name"),
            attachments=self._extract_attachments(fields),
            epic=self._extract_epic(fields),
        )

    # -- private helpers ----------------------------------------------------

    @staticmethod
    def _extract_description(fields: Dict[str, Any]) -> str:
        raw = fields.get("description") or ""
        if isinstance(raw, dict):
            # Atlassian Document Format (ADF) – flatten to text
            return _adf_to_text(raw)
        return raw

    @staticmethod
    def _extract_linked_issues(fields: Dict[str, Any]) -> List[str]:
        linked: List[str] = []
        for link in fields.get("issuelinks", []):
            if outward := link.get("outwardIssue"):
                linked.append(outward["key"])
            elif inward := link.get("inwardIssue"):
                linked.append(inward["key"])
        return linked

    @staticmethod
    def _extract_comments(fields: Dict[str, Any]) -> List[str]:
        comment_block = safe_get(fields, "comment", "comments") or []
        results = []
        for c in comment_block:
            body = c.get("body", "")
            if not body:
                continue
            # body may be ADF dict (Jira Cloud v3) or plain string (v2)
            text = _adf_to_text(body) if isinstance(body, dict) else sanitize_text(body)
            if text:
                results.append(text)
        return results

    @staticmethod
    def _extract_attachments(fields: Dict[str, Any]) -> List[str]:
        return [a.get("filename", "") for a in fields.get("attachment", []) if a.get("filename")]

    @staticmethod
    def _extract_epic(fields: Dict[str, Any]) -> Optional[str]:
        # Jira Server customfield_10014 is a common epic-link field;
        # Jira Cloud uses "epic" or "parent".
        for key in ("epic", "parent"):
            if val := fields.get(key):
                if isinstance(val, dict):
                    return val.get("key") or val.get("name")
                return str(val)
        # Fallback to common custom field
        if epic_cf := fields.get("customfield_10014"):
            return str(epic_cf)
        return None


def _adf_to_text(node: Any) -> str:
    """Recursively flatten Atlassian Document Format JSON to plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        children = node.get("content", [])
        parts = [_adf_to_text(c) for c in children]
        separator = "\n" if node.get("type") in ("paragraph", "listItem", "orderedList", "bulletList") else ""
        return separator.join(parts)
    if isinstance(node, list):
        return "\n".join(_adf_to_text(item) for item in node)
    return ""
