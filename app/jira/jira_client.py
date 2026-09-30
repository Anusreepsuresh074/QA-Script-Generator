"""
Low-level HTTP client for the Jira REST API.

Tries API v3 first (required for next-gen / team-managed projects on Jira Cloud),
then falls back to v2.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth

from app.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_API_VERSIONS = ("/rest/api/3", "/rest/api/2")


class JiraAuthenticationError(Exception):
    """Raised when Jira credentials are invalid."""


class JiraConnectionError(Exception):
    """Raised when Jira is unreachable."""


class JiraTicketNotFoundError(Exception):
    """Raised when a ticket key does not exist."""


class JiraClient:
    """Thin wrapper around the Jira REST API (v3 with v2 fallback)."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._base_url = self._settings.jira_url.rstrip("/")
        self._auth = HTTPBasicAuth(
            self._settings.jira_email,
            self._settings.jira_api_token,
        )
        self._timeout = self._settings.jira_request_timeout
        self._session = requests.Session()
        self._session.auth = self._auth
        self._session.headers.update({"Accept": "application/json"})

    # -- public helpers -----------------------------------------------------

    def connect(self) -> bool:
        """Verify that the credentials are valid by hitting ``/myself``.

        Returns ``True`` on success; raises on failure.
        """
        for api in _API_VERSIONS:
            url = f"{self._base_url}{api}/myself"
            logger.info("Verifying Jira connection → %s", url)
            try:
                resp = self._request("GET", url)
                logger.info("Authenticated as %s (via %s)", resp.get("displayName", "unknown"), api)
                return True
            except JiraTicketNotFoundError:
                continue
        raise JiraConnectionError("Could not verify connection on any API version")

    def fetch_ticket(self, ticket_id: str) -> Dict[str, Any]:
        """Fetch a single Jira issue by key (e.g. ``PROJ-123``).

        Tries v3 first, falls back to v2.
        """
        logger.info("Fetching ticket %s", ticket_id)
        last_exc: Optional[Exception] = None
        for api in _API_VERSIONS:
            url = f"{self._base_url}{api}/issue/{ticket_id}?expand=renderedFields"
            logger.debug("Trying %s", url)
            try:
                return self._request("GET", url)
            except JiraTicketNotFoundError as exc:
                logger.debug("Not found via %s, trying next version", api)
                last_exc = exc
        raise last_exc or JiraTicketNotFoundError(f"Ticket {ticket_id} not found")

    def fetch_multiple_tickets(self, jql: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """Run a JQL query and return the matching issues."""
        logger.info("Searching Jira with JQL: %s", jql)
        last_exc: Optional[Exception] = None
        for api in _API_VERSIONS:
            url = f"{self._base_url}{api}/search"
            params = {"jql": jql, "maxResults": max_results, "expand": "renderedFields"}
            try:
                data = self._request("GET", url, params=params)
                issues: List[Dict[str, Any]] = data.get("issues", [])
                logger.info("JQL returned %d issue(s) (via %s)", len(issues), api)
                return issues
            except JiraTicketNotFoundError as exc:
                last_exc = exc
        raise last_exc or JiraTicketNotFoundError(f"Search failed for: {jql}")

    def add_comment(self, ticket_id: str, adf_body: Dict[str, Any]) -> Dict[str, Any]:
        """Post an ADF comment to a Jira issue."""
        url = f"{self._base_url}/rest/api/3/issue/{ticket_id}/comment"
        resp = self._session.post(
            url,
            json={"body": adf_body},
            headers={"Content-Type": "application/json"},
            timeout=self._timeout,
        )
        if resp.status_code == 401:
            raise JiraAuthenticationError("Invalid Jira credentials")
        if resp.status_code >= 400:
            logger.error("Add comment error %d: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return resp.json()

    def add_attachment(self, ticket_id: str, filename: str, content: bytes) -> List[Dict[str, Any]]:
        """Attach a file to a Jira issue."""
        url = f"{self._base_url}/rest/api/3/issue/{ticket_id}/attachments"
        resp = self._session.post(
            url,
            files={"file": (filename, content, "text/csv")},
            headers={"X-Atlassian-Token": "no-check"},
            timeout=self._timeout,
        )
        if resp.status_code == 401:
            raise JiraAuthenticationError("Invalid Jira credentials")
        if resp.status_code >= 400:
            logger.error("Add attachment error %d: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return resp.json()

    # -- internal -----------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            resp = self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=self._timeout,
            )
        except requests.ConnectionError as exc:
            logger.error("Cannot reach Jira at %s: %s", url, exc)
            raise JiraConnectionError(f"Cannot reach Jira: {exc}") from exc
        except requests.Timeout as exc:
            logger.error("Jira request timed out: %s", exc)
            raise JiraConnectionError(f"Jira request timed out: {exc}") from exc

        if resp.status_code == 401:
            logger.error("Jira authentication failed (401)")
            raise JiraAuthenticationError("Invalid Jira credentials")
        if resp.status_code == 404:
            logger.warning("Jira resource not found: %s", url)
            raise JiraTicketNotFoundError(f"Not found: {url}")
        if resp.status_code >= 400:
            logger.error("Jira API error %d: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()

        return resp.json()
