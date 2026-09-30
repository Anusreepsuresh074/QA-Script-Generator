"""
Parse Swagger 2.0 and OpenAPI 3.x specifications.

Accepts a URL, a local file path, or an already-loaded dict.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests
import yaml

from app.config.settings import Settings, get_settings
from app.models.schemas import APISchema
from app.utils.helpers import load_spec_file, safe_get

logger = logging.getLogger(__name__)


class SwaggerParseError(Exception):
    """Raised when the spec cannot be loaded or is structurally invalid."""


class SwaggerParser:
    """Loads and parses OpenAPI / Swagger specs into ``APISchema`` objects."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._timeout = self._settings.swagger_request_timeout

    # -- public API ---------------------------------------------------------

    def parse(
        self,
        source: Union[str, Dict[str, Any]],
    ) -> List[APISchema]:
        """Parse APIs from a URL, file path, or raw dict.

        Returns a list of ``APISchema`` instances.
        """
        spec = self._load(source)
        self._validate_spec(spec)
        auth_type = self._detect_auth(spec)
        return self._extract_endpoints(spec, auth_type)

    # -- loading ------------------------------------------------------------

    def _load(self, source: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        if isinstance(source, dict):
            logger.info("Using inline spec dict")
            return source

        source_str: str = source
        if source_str.startswith(("http://", "https://")):
            return self._load_from_url(source_str)
        return self._load_from_file(source_str)

    def _load_from_url(self, url: str) -> Dict[str, Any]:
        logger.info("Fetching spec from URL: %s", url)
        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Failed to fetch Swagger spec: %s", exc)
            raise SwaggerParseError(f"Cannot fetch spec from {url}: {exc}") from exc

        content_type = resp.headers.get("Content-Type", "")
        try:
            if "yaml" in content_type or url.endswith((".yaml", ".yml")):
                return yaml.safe_load(resp.text)
            return resp.json()
        except Exception as exc:
            raise SwaggerParseError(f"Cannot decode response from {url}: {exc}") from exc

    @staticmethod
    def _load_from_file(path: str) -> Dict[str, Any]:
        logger.info("Loading spec from file: %s", path)
        try:
            return load_spec_file(path)
        except (FileNotFoundError, ValueError) as exc:
            raise SwaggerParseError(str(exc)) from exc

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _validate_spec(spec: Dict[str, Any]) -> None:
        if "paths" not in spec:
            raise SwaggerParseError("Spec has no 'paths' key – possibly malformed")
        is_swagger2 = spec.get("swagger", "").startswith("2")
        is_openapi3 = spec.get("openapi", "").startswith("3")
        if not is_swagger2 and not is_openapi3:
            logger.warning(
                "Spec version not detected; will attempt best-effort parsing"
            )

    # -- extraction ---------------------------------------------------------

    def _extract_endpoints(
        self,
        spec: Dict[str, Any],
        auth_type: Optional[str],
    ) -> List[APISchema]:
        endpoints: List[APISchema] = []
        paths: Dict[str, Any] = spec.get("paths", {})

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.upper() not in {
                    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"
                }:
                    continue
                if not isinstance(operation, dict):
                    continue

                endpoints.append(
                    self._build_api_schema(
                        spec, path, method.upper(), operation, auth_type,
                    )
                )

        logger.info("Parsed %d endpoint(s) from spec", len(endpoints))
        return endpoints

    def _build_api_schema(
        self,
        spec: Dict[str, Any],
        path: str,
        method: str,
        operation: Dict[str, Any],
        auth_type: Optional[str],
    ) -> APISchema:
        parameters = self._resolve_parameters(spec, operation)
        request_body, required_fields = self._resolve_request_body(spec, operation)
        response_codes = [str(c) for c in operation.get("responses", {}).keys()]
        examples = self._resolve_examples(operation, request_body)

        return APISchema(
            endpoint=path,
            method=method,
            summary=operation.get("summary") or operation.get("description") or "",
            parameters=parameters,
            request_body=request_body,
            required_fields=required_fields,
            response_codes=response_codes,
            authentication_type=auth_type,
            examples=examples,
        )

    # -- parameter / body resolution ----------------------------------------

    @staticmethod
    def _resolve_parameters(
        spec: Dict[str, Any], operation: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        raw_params = operation.get("parameters", [])
        params: List[Dict[str, Any]] = []
        for p in raw_params:
            if "$ref" in p:
                p = _resolve_ref(spec, p["$ref"])
            if not isinstance(p, dict):
                continue
            params.append({
                "name": p.get("name"),
                "in": p.get("in"),
                "required": p.get("required", False),
                "type": p.get("type") or safe_get(p, "schema", "type"),
            })
        return params

    @staticmethod
    def _resolve_request_body(
        spec: Dict[str, Any], operation: Dict[str, Any]
    ) -> tuple[Optional[Dict[str, Any]], List[str]]:
        required_fields: List[str] = []

        # OpenAPI 3.x
        rb = operation.get("requestBody")
        if rb:
            if "$ref" in rb:
                rb = _resolve_ref(spec, rb["$ref"])
            content = rb.get("content", {})
            for media, media_obj in content.items():
                schema = media_obj.get("schema", {})
                if "$ref" in schema:
                    schema = _resolve_ref(spec, schema["$ref"])
                required_fields = schema.get("required", [])
                return schema, required_fields

        # Swagger 2.x body parameter
        for p in operation.get("parameters", []):
            if p.get("in") == "body":
                schema = p.get("schema", {})
                if "$ref" in schema:
                    schema = _resolve_ref(spec, schema["$ref"])
                required_fields = schema.get("required", [])
                return schema, required_fields

        return None, required_fields

    @staticmethod
    def _resolve_examples(
        operation: Dict[str, Any],
        body_schema: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        examples: Dict[str, Any] = {}
        if body_schema and "example" in body_schema:
            examples["request"] = body_schema["example"]
        for code, resp in operation.get("responses", {}).items():
            if isinstance(resp, dict) and "example" in resp:
                examples[str(code)] = resp["example"]
            elif isinstance(resp, dict):
                ex = safe_get(resp, "content", "application/json", "example")
                if ex:
                    examples[str(code)] = ex
        return examples or None

    # -- auth detection -----------------------------------------------------

    @staticmethod
    def _detect_auth(spec: Dict[str, Any]) -> Optional[str]:
        # OpenAPI 3.x
        schemes = safe_get(spec, "components", "securitySchemes")
        if not schemes:
            # Swagger 2.x
            schemes = spec.get("securityDefinitions")
        if not schemes or not isinstance(schemes, dict):
            return None
        types = {v.get("type") for v in schemes.values() if isinstance(v, dict)}
        return ", ".join(sorted(types)) if types else None


def _resolve_ref(spec: Dict[str, Any], ref: str) -> Dict[str, Any]:
    """Follow a ``$ref`` pointer (only local JSON-pointer refs are supported)."""
    if not ref.startswith("#/"):
        return {}
    parts = ref.lstrip("#/").split("/")
    node: Any = spec
    for part in parts:
        if isinstance(node, dict):
            node = node.get(part, {})
        else:
            return {}
    return node if isinstance(node, dict) else {}
