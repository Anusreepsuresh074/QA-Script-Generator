"""Jira REST API v3 client: issue creation, user lookup, retries."""

from __future__ import annotations

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
from app.models import GeminiQAAnalysis, JiraCreatedIssue

logger = logging.getLogger(__name__)

_ASSIGNEE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text or " "}],
    }


def _heading(level: int, text: str) -> dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def build_issue_description_adf(analysis: GeminiQAAnalysis) -> dict[str, Any]:
    """
    Build Atlassian Document Format (ADF) for Jira Cloud description field.
    Mirrors the requested ticket layout: Summary sections as structured text.
    """
    steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(analysis.steps_to_reproduce)) or (
        "(Not specified — see Slack thread)"
    )
    blocks: list[dict[str, Any]] = [
        _heading(2, "Environment"),
        _paragraph(analysis.environment or "See Slack / to be confirmed"),
        _heading(2, "Steps to Reproduce"),
        _paragraph(steps),
        _heading(2, "Expected Result"),
        _paragraph(analysis.expected_result or "Reasonable expected behavior per QA analysis."),
        _heading(2, "Actual Result"),
        _paragraph(analysis.actual_result or analysis.short_summary),
        _heading(2, "Impact"),
        _paragraph(analysis.impact or "Impact to be validated with reporter."),
        _heading(2, "Priority / Severity (AI-suggested)"),
        _paragraph(f"Priority: {analysis.priority}\nSeverity: {analysis.severity}"),
        _heading(2, "Module / Platform"),
        _paragraph(
            f"Module: {analysis.module or 'Unknown'}\nPlatform: {analysis.platform or 'Unknown'}"
        ),
    ]
    if (analysis.assignee_hint or "").strip():
        blocks.append(_heading(2, "Requested assignee (from Slack)"))
        blocks.append(_paragraph(analysis.assignee_hint.strip()))
    if (analysis.sprint_name or "").strip():
        blocks.append(_heading(2, "Requested sprint (from Slack)"))
        blocks.append(_paragraph(analysis.sprint_name.strip()))
    rep = (analysis.reporter_hint or "").strip()
    if rep:
        blocks.append(_heading(2, "Reporter"))
        blocks.append(_paragraph(rep))
    return {"type": "doc", "version": 1, "content": blocks}


class JiraServiceError(Exception):
    """Raised for unexpected Jira API responses."""


class JiraService:
    """Async HTTP client for Jira Cloud."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=f"{settings.jira_base_url}/rest/api/3",
            auth=(settings.jira_email, settings.jira_api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=httpx.Timeout(float(settings.jira_request_timeout)),
        )
        self._issue_types_cache: dict[str, list[str]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        response = await self._client.get(path, params=params or {})
        if response.status_code >= 400:
            logger.error(
                "Jira API error %s GET %s: %s",
                response.status_code,
                path,
                response.text[:2000],
            )
        response.raise_for_status()
        return response

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _post(self, path: str, json_body: dict[str, Any]) -> httpx.Response:
        response = await self._client.post(path, json=json_body)
        if response.status_code >= 400:
            logger.error(
                "Jira API error %s POST %s: %s",
                response.status_code,
                path,
                response.text[:2000],
            )
        response.raise_for_status()
        return response

    async def get_project_issue_types(self, project_key: str) -> list[str]:
        """Fetch and cache valid issue type names for the given project key."""
        if project_key not in self._issue_types_cache:
            r = await self._get(f"/project/{project_key}")
            self._issue_types_cache[project_key] = [
                it["name"] for it in r.json().get("issueTypes", [])
            ]
        return self._issue_types_cache[project_key]

    def _resolve_issue_type(self, requested: str, available: list[str]) -> str:
        """Case-insensitive match of requested type against available types. Raises if not found."""
        for name in available:
            if name.lower() == requested.lower():
                return name
        available_str = ", ".join(available)
        raise JiraServiceError(
            f"Issue type '{requested}' is not available in project '{self._settings.jira_project_key}'. "
            f"Available types: {available_str}"
        )

    async def find_account_id_by_email(self, email: str) -> str | None:
        """Resolve Jira Cloud assignee accountId from email."""
        r = await self._get("/user/search", params={"query": email.strip()})
        users = r.json()
        if not isinstance(users, list) or not users:
            return None
        first = users[0]
        return str(first.get("accountId", "")) or None

    async def resolve_assignee_account_id(self, hint: str) -> str | None:
        """
        Resolve Jira assignee from Slack-derived hint: explicit email, or name / search query.
        """
        h = (hint or "").strip()
        if not h:
            return None
        m = _ASSIGNEE_EMAIL.search(h)
        if m:
            uid = await self.find_account_id_by_email(m.group(0))
            if uid:
                return uid
        q = h.lstrip("@").strip()[:255]
        if not q:
            return None
        r = await self._get("/user/search", params={"query": q})
        users = r.json()
        if not isinstance(users, list) or not users:
            return None
        hl = q.lower()

        def email_local(em: str) -> str:
            em = (em or "").strip().lower()
            return em.split("@", 1)[0] if "@" in em else ""

        for u in users:
            dn = (u.get("displayName") or "").strip().lower()
            if dn and dn == hl:
                return str(u.get("accountId") or "") or None
        for u in users:
            dn = (u.get("displayName") or "").strip().lower()
            if dn.startswith(hl):
                return str(u.get("accountId") or "") or None
        for u in users:
            em = (u.get("emailAddress") or "").strip()
            if email_local(em) == hl:
                return str(u.get("accountId") or "") or None
        for u in users:
            dn = (u.get("displayName") or "").strip().lower()
            if hl in dn:
                return str(u.get("accountId") or "") or None
        return str(users[0].get("accountId") or "") or None

    async def _agile_get(self, subpath: str, params: dict[str, Any] | None = None) -> httpx.Response:
        root = self._settings.jira_base_url.rstrip("/")
        path = subpath if subpath.startswith("/") else f"/{subpath}"
        url = f"{root}/rest/agile/1.0{path}"
        response = await self._client.get(url, params=params or {})
        if response.status_code >= 400:
            logger.error(
                "Jira Agile error %s GET %s: %s",
                response.status_code,
                url,
                response.text[:1500],
            )
        response.raise_for_status()
        return response

    async def _agile_post(self, subpath: str, json_body: dict[str, Any]) -> httpx.Response:
        root = self._settings.jira_base_url.rstrip("/")
        path = subpath if subpath.startswith("/") else f"/{subpath}"
        url = f"{root}/rest/agile/1.0{path}"
        response = await self._client.post(url, json=json_body)
        if response.status_code >= 400:
            logger.error(
                "Jira Agile error %s POST %s: %s",
                response.status_code,
                url,
                response.text[:1500],
            )
        response.raise_for_status()
        return response

    async def find_sprint_id_by_name(self, sprint_name: str) -> tuple[int | None, str | None]:
        """Match sprint name (substring, case-insensitive) against active/future sprints on project boards."""
        needle = (sprint_name or "").strip().lower()
        if not needle:
            return None, None
        try:
            br = await self._agile_get(
                "/board",
                {"projectKeyOrId": self._settings.jira_project_key},
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("Could not list Agile boards: %s", exc.response.text[:500])
            return None, None
        for board in br.json().get("values") or []:
            bid = board.get("id")
            if bid is None:
                continue
            try:
                sr = await self._agile_get(
                    f"/board/{bid}/sprint",
                    {"state": "active,future", "maxResults": 50},
                )
            except httpx.HTTPStatusError:
                continue
            for sp in sr.json().get("values") or []:
                raw = (sp.get("name") or "").strip()
                low = raw.lower()
                if not low:
                    continue
                if needle == low or needle in low or low in needle:
                    sid = sp.get("id")
                    if sid is not None:
                        return int(sid), raw
        return None, None

    def _build_labels(self, analysis: GeminiQAAnalysis) -> list[str]:
        """Auto labels: category, module slug, platform, agent tag."""
        base = {
            "qa-agent",
            analysis.issue_category,
            *[l.replace(" ", "-").lower() for l in analysis.suggested_labels],
        }
        if analysis.module:
            base.add(f"module-{analysis.module.replace(' ', '-').lower()[:40]}")
        if analysis.platform:
            base.add(f"platform-{analysis.platform.replace(' ', '-').lower()[:40]}")
        # Jira label constraints: alphanumeric, dash, underscore
        cleaned: list[str] = []
        for raw in base:
            s = "".join(c if c.isalnum() or c in "-_" else "-" for c in raw).strip("-")
            if s and s not in cleaned and len(s) <= 60:
                cleaned.append(s[:60])
        return cleaned[:20]

    def _resolve_assignee_email(self, analysis: GeminiQAAnalysis) -> str | None:
        """Pick assignee email from module/platform keyword map."""
        mapping = self._settings.jira_module_assignee_map
        if not mapping:
            return None
        keys = [analysis.module.lower(), analysis.platform.lower()]
        for hint in keys:
            if not hint:
                continue
            for mod_key, email in mapping.items():
                if mod_key in hint or hint in mod_key:
                    return email
        return None

    async def create_issue_from_analysis(
        self,
        analysis: GeminiQAAnalysis,
    ) -> JiraCreatedIssue:
        """Create a Jira issue; priority/assignee are best-effort for any Jira shape."""
        description = build_issue_description_adf(analysis)
        default_type = (self._settings.jira_issue_type or "Task").strip()
        override = (analysis.jira_issue_type_override or "").strip()
        requested_type = override or default_type

        available_types = await self.get_project_issue_types(self._settings.jira_project_key)
        issue_type_name = self._resolve_issue_type(requested_type, available_types)

        fields: dict[str, Any] = {
            "project": {"key": self._settings.jira_project_key},
            "summary": analysis.jira_summary[:254],
            "description": description,
            "issuetype": {"name": issue_type_name},
            "labels": self._build_labels(analysis),
        }
        fields["priority"] = {"name": analysis.priority}

        assignee_account_id: str | None = None
        slack_hint = (analysis.assignee_hint or "").strip()
        if slack_hint:
            assignee_account_id = await self.resolve_assignee_account_id(slack_hint)
        if not assignee_account_id:
            assignee_email = self._resolve_assignee_email(analysis)
            if assignee_email:
                assignee_account_id = await self.find_account_id_by_email(assignee_email)
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}

        reporter_account_id: str | None = None
        reporter_hint = (analysis.reporter_hint or "").strip()
        if reporter_hint:
            reporter_account_id = await self.resolve_assignee_account_id(reporter_hint)
        if reporter_account_id:
            fields["reporter"] = {"accountId": reporter_account_id}

        tried_strip_priority = False
        tried_strip_assignee = False
        tried_strip_reporter = False
        r: httpx.Response | None = None
        for _ in range(5):
            try:
                r = await self._post("/issue", {"fields": fields})
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
                body = (exc.response.text or "").lower()
                if (
                    not tried_strip_priority
                    and "priority" in fields
                    and ("priority" in body or "field 'priority'" in body)
                ):
                    logger.warning("Jira rejected priority; retrying without priority field")
                    fields.pop("priority", None)
                    tried_strip_priority = True
                    continue
                if not tried_strip_assignee and "assignee" in fields and "assignee" in body:
                    logger.warning("Jira rejected assignee; retrying without assignee field")
                    fields.pop("assignee", None)
                    assignee_account_id = None
                    tried_strip_assignee = True
                    continue
                if not tried_strip_reporter and "reporter" in fields and "reporter" in body:
                    logger.warning("Jira rejected reporter (no Modify Reporter permission); retrying without reporter field")
                    fields.pop("reporter", None)
                    reporter_account_id = None
                    tried_strip_reporter = True
                    continue
                raise
        if r is None:
            raise JiraServiceError("Jira create failed after retries")

        data = r.json()
        key = data.get("key")
        if not key:
            raise JiraServiceError(f"Unexpected create response: {data}")
        self_url = str(data.get("self", ""))

        sprint_matched_name: str | None = None
        sprint_label = (analysis.sprint_name or "").strip()
        if sprint_label:
            sprint_id, matched_name = await self.find_sprint_id_by_name(sprint_label)
            if sprint_id is not None:
                try:
                    await self._agile_post(
                        f"/sprint/{sprint_id}/issue",
                        {"issues": [key]},
                    )
                    sprint_matched_name = matched_name
                    logger.info("Added %s to sprint %s (id=%s)", key, matched_name, sprint_id)
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "Could not add %s to sprint %r: %s",
                        key,
                        matched_name,
                        exc.response.text[:800],
                    )
            else:
                logger.warning(
                    "No active/future sprint matched %r for project %s",
                    sprint_label,
                    self._settings.jira_project_key,
                )

        assignee_name: str | None = None
        assignee_id: str | None = assignee_account_id
        reporter_name: str | None = None
        try:
            detail = await self._get(f"/issue/{key}", params={"fields": "assignee,reporter"})
            issue_fields = detail.json().get("fields", {})
            assignee_obj = issue_fields.get("assignee")
            if isinstance(assignee_obj, dict):
                assignee_name = assignee_obj.get("displayName")
                assignee_id = assignee_obj.get("accountId", assignee_id)
            reporter_obj = issue_fields.get("reporter")
            if isinstance(reporter_obj, dict):
                reporter_name = reporter_obj.get("displayName")
        except httpx.HTTPError as exc:
            logger.debug("Could not load issue fields for %s: %s", key, exc)

        return JiraCreatedIssue(
            key=key,
            self_url=self_url,
            assignee_display_name=assignee_name,
            assignee_account_id=str(assignee_id) if assignee_id else None,
            reporter_display_name=reporter_name,
            sprint_matched_name=sprint_matched_name,
        )
