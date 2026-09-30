"""
Extract acceptance criteria from Jira ticket descriptions.

Strategy:
  1. Regex-based extraction for well-structured descriptions
     (handles sub-grouped criteria under headings like "Create User:").
  2. Fallback heuristic extraction for loosely formatted text.
  3. An ``llm_ready_payload`` helper packages the raw text for
     downstream LLM parsing when regex extraction yields nothing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import List, Optional

from app.ai.llm_client import LLMClient, LLMTransientError
from app.config.settings import MappingType, Settings, get_settings

logger = logging.getLogger(__name__)

_AC_HEADERS = re.compile(
    r"(?:acceptance\s*criteria|^ac)\s*[:：\-]?\s*",
    re.IGNORECASE | re.MULTILINE,
)

_LIST_ITEM = re.compile(
    r"^\s*(?:\d+[\.\)]\s*|[-*•]\s*|>\s*)(.+)",
    re.MULTILINE,
)

_SUB_HEADING = re.compile(r"^[A-Za-z][A-Za-z0-9 ]+:\s*$")

_NOISE = re.compile(
    r"^(?:\d{3}|/[\w/{}]+|https?://\S+)$"
)

_MIN_CRITERION_WORDS = 3


class CriteriaExtractor:
    """Stateless helper that pulls acceptance criteria out of free-form text."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    def extract(self, text: Optional[str], summary: str = "") -> List[str]:
        """Return a list of acceptance criteria strings.

        Falls back to heuristic extraction when no structured block is found.
        When ``mapping_type`` is ``ollama`` or ``groq`` and regex/heuristic find nothing,
        uses the LLM (Ollama first, Groq fallback) to extract criteria from the ticket text.
        """
        if not text:
            logger.warning("Empty text passed to criteria extractor")
            return self._maybe_llm_extract("", summary)

        criteria = self._regex_extract(text)
        if criteria:
            logger.info("Regex extraction yielded %d criteria", len(criteria))
            return criteria

        criteria = self._heuristic_extract(text)
        if criteria:
            logger.info("Heuristic extraction yielded %d criteria", len(criteria))
            return criteria

        logger.warning("No acceptance criteria found – trying LLM extraction")
        return self._maybe_llm_extract(text, summary)

    def llm_ready_payload(self, text: str) -> dict:
        """Package the raw text for an LLM extraction prompt."""
        return {
            "instruction": (
                "Extract all acceptance criteria from the following Jira ticket "
                "description. Return them as a JSON array of strings."
            ),
            "text": text,
        }

    def _maybe_llm_extract(self, text: str, summary: str) -> List[str]:
        if self._settings.mapping_type not in (MappingType.OLLAMA, MappingType.GROQ):
            return []
        if not text and not summary:
            return []
        return self._llm_extract(text, summary)

    _LLM_CRITERIA_SYSTEM = """You extract acceptance criteria from Jira ticket text.

Return ONLY valid JSON:
{
  "acceptance_criteria": ["criterion 1", "criterion 2"]
}

Rules:
- Each criterion must be a clear, testable statement
- Preserve wording from the ticket when possible
- If no criteria exist, return an empty array
- Do not include markdown or explanation"""

    def _llm_extract(self, text: str, summary: str) -> List[str]:
        llm = LLMClient(self._settings)
        user_prompt = f"Summary: {summary or 'N/A'}\n\nDescription:\n{text or 'N/A'}"
        try:
            raw = llm.chat_completion(
                system=self._LLM_CRITERIA_SYSTEM,
                user=user_prompt,
                temperature=0.1,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
        except LLMTransientError as exc:
            logger.error("LLM criteria extraction failed: %s", exc)
            return []

        try:
            data = json.loads(raw.strip())
            items = data.get("acceptance_criteria", [])
            if not isinstance(items, list):
                return []
            criteria = [str(item).strip() for item in items if str(item).strip()]
            logger.info("LLM extraction (%s) yielded %d criteria", llm.last_provider, len(criteria))
            return criteria
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error("Could not parse LLM criteria response: %s", exc)
            return []

    # -- private strategies -------------------------------------------------

    @staticmethod
    def _regex_extract(text: str) -> List[str]:
        """Look for a clearly labelled AC block and pull all criteria from it,
        including items under sub-headings like 'Create User:'."""
        match = _AC_HEADERS.search(text)
        if not match:
            return []

        after_header = text[match.end():]
        current_group: Optional[str] = None
        criteria: List[str] = []

        for line in after_header.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            if _SUB_HEADING.match(stripped):
                current_group = stripped.rstrip(": ").strip()
                continue

            m = _LIST_ITEM.match(stripped)
            item = (m.group(1).strip() if m else stripped)

            if not _is_valid_criterion(item):
                continue

            if current_group:
                item = f"{current_group} – {item}"

            criteria.append(item)

        return criteria

    @staticmethod
    def _heuristic_extract(text: str) -> List[str]:
        """Fallback: grab any numbered / bulleted list items from the text."""
        items = _LIST_ITEM.findall(text)
        return [i.strip() for i in items if _is_valid_criterion(i.strip())]


def _is_valid_criterion(text: str) -> bool:
    """Filter out noise like bare status codes, URLs, or single words."""
    if not text:
        return False
    if _NOISE.match(text):
        return False
    if len(text.split()) < _MIN_CRITERION_WORDS:
        return False
    return True
