"""Postman Collection v2.1 templates and QA standards for script generation."""

from __future__ import annotations

import json

from app.models.schemas import APIType


# ---------------------------------------------------------------------------
# Postman collection skeleton (used by the LLM prompt)
# ---------------------------------------------------------------------------

def postman_collection_skeleton(ticket_id: str, summary: str, base_url: str) -> str:
    """Return a JSON skeleton showing the LLM the expected collection structure."""
    skeleton = {
        "info": {
            "name": f"{ticket_id} — {summary}",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "base_url", "value": base_url},
            {"key": "api_key", "value": ""},
        ],
        "item": [
            {
                "name": "EXAMPLE: positive scenario name",
                "request": {
                    "method": "GET",
                    "url": {
                        "raw": "{{base_url}}/endpoint",
                        "host": ["{{base_url}}"],
                        "path": ["endpoint"],
                    },
                    "header": [
                        {"key": "Content-Type", "value": "application/json"},
                        {"key": "Authorization", "value": "Bearer {{api_key}}"},
                    ],
                },
                "event": [
                    {
                        "listen": "test",
                        "script": {
                            "type": "text/javascript",
                            "exec": [
                                "pm.test('Status is 200', function () {",
                                "  pm.response.to.have.status(200);",
                                "});",
                                "pm.test('Response has data', function () {",
                                "  const body = pm.response.json();",
                                "  pm.expect(body).to.have.property('data');",
                                "});",
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(skeleton, indent=2)


def validate_postman_collection(source: str) -> None:
    """Raise ValueError if source is not a valid Postman collection JSON."""
    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Generated Postman collection is not valid JSON: {exc}") from exc
    if "item" not in data:
        raise ValueError("Generated Postman collection is missing 'item' array")
    if "info" not in data:
        raise ValueError("Generated Postman collection is missing 'info' object")


def postman_collection_filename(ticket_id: str) -> str:
    return f"{ticket_id.lower().replace('-', '_')}_collection.json"


# ---------------------------------------------------------------------------
# QA standards prompts
# ---------------------------------------------------------------------------

_REST_POSTMAN_STANDARDS = """Internal QA automation standards for Postman Collection v2.1 + REST APIs (MUST follow exactly):
- Output ONLY a single valid Postman Collection v2.1 JSON object. No markdown fences, no explanation.
- The top-level object must have exactly: "info", "variable", "item".
- "info": set "name" to "<ticket_id> — <summary>" and "schema" to "https://schema.getpostman.com/json/collection/v2.1.0/collection.json".
- "variable": include at minimum [{"key":"base_url","value":"<base_url>"}, {"key":"api_key","value":""}].
- "item": array of request objects — one per scenario.
- Each item must have: "name" (scenario name), "request", "event".
- "request": include "method", "url" object (with "raw", "host", "path"), and "header" array.
- Use {{base_url}} variable in every URL raw string (never hardcode the base URL).
- "event": array with one object: {"listen":"test","script":{"type":"text/javascript","exec":[...lines...]}}.
- Test scripts use pm.test(), pm.response.to.have.status(), pm.expect(), pm.response.json().
- Positive tests: assert correct HTTP status and expected response fields.
- Negative tests: assert error HTTP status (4xx) OR assert error field in response body.
- Edge tests: assert boundary behaviour in the response.
- DO NOT output any text outside the JSON object."""

_GRAPHQL_POSTMAN_STANDARDS = """Internal QA automation standards for Postman Collection v2.1 + GraphQL (MUST follow exactly):
- Output ONLY a single valid Postman Collection v2.1 JSON object. No markdown fences, no explanation.
- Top-level: "info", "variable", "item".
- "variable": [{"key":"graphql_url","value":"<url>"},{"key":"api_key","value":""}].
- All requests use POST method to {{graphql_url}}.
- "request.body": {"mode":"graphql","graphql":{"query":"...","variables":"{}"}}.
- GraphQL always returns HTTP 200; test scripts check response.data for success, response.errors for failures.
- Positive pm.test: pm.expect(jsonData).to.not.have.property('errors');
- Negative pm.test: pm.expect(jsonData).to.have.property('errors');
- DO NOT output any text outside the JSON object."""

POSTMAN_STANDARDS_BY_TYPE: dict[APIType, str] = {
    APIType.REST: _REST_POSTMAN_STANDARDS,
    APIType.GRAPHQL: _GRAPHQL_POSTMAN_STANDARDS,
    APIType.SOAP: _REST_POSTMAN_STANDARDS,       # SOAP via raw body (fallback)
    APIType.GRPC: _REST_POSTMAN_STANDARDS,       # gRPC-web (fallback)
    APIType.WEBSOCKET: _REST_POSTMAN_STANDARDS,  # WebSocket (limited Postman support)
}
