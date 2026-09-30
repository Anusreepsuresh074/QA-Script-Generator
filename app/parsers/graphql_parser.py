"""
Parse GraphQL schemas into APISchema objects.

Accepts:
  - SDL text (Schema Definition Language with type Query/Mutation/Subscription blocks)
  - GraphQL introspection JSON ({"data": {"__schema": {...}}})
  - URL to a .graphql/.gql file or an introspection endpoint
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

import requests
import yaml

from app.models.schemas import APISchema, APIType
from app.parsers.base_parser import BaseSpecParser

logger = logging.getLogger(__name__)

_INTROSPECTION_QUERY = """
{
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types {
      name
      kind
      fields(includeDeprecated: false) {
        name
        description
        args { name type { name kind ofType { name kind } } }
        type { name kind ofType { name kind } }
      }
    }
  }
}
"""


class GraphQLParseError(Exception):
    pass


class GraphQLParser(BaseSpecParser):
    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def parse(self, source: Union[str, Dict[str, Any]]) -> List[APISchema]:
        if isinstance(source, dict):
            return self._from_introspection(source)

        text: str = source
        if text.startswith(("http://", "https://")):
            return self._from_url(text)

        # inline text — SDL or introspection JSON
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                return self._from_introspection(data)
            except json.JSONDecodeError:
                pass
        return self._from_sdl(stripped)

    # -- loaders ----------------------------------------------------------------

    def _from_url(self, url: str) -> List[APISchema]:
        lower = url.lower()
        if lower.endswith((".graphql", ".gql")):
            return self._fetch_sdl(url)
        # Try introspection query against the endpoint
        try:
            resp = requests.post(
                url,
                json={"query": _INTROSPECTION_QUERY},
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return self._from_introspection(resp.json())
        except Exception:
            # Fallback: try fetching as raw SDL file
            return self._fetch_sdl(url)

    def _fetch_sdl(self, url: str) -> List[APISchema]:
        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
            return self._from_sdl(resp.text)
        except Exception as exc:
            raise GraphQLParseError(f"Cannot fetch GraphQL schema from {url}: {exc}") from exc

    # -- SDL parsing ------------------------------------------------------------

    def _from_sdl(self, sdl: str) -> List[APISchema]:
        operations: List[APISchema] = []

        # Remove block comments
        sdl = re.sub(r'""".*?"""', "", sdl, flags=re.DOTALL)
        sdl = re.sub(r'#[^\n]*', "", sdl)

        for op_type in ("Query", "Mutation", "Subscription"):
            block = _extract_type_block(sdl, op_type)
            if not block:
                continue
            for field in _parse_fields(block):
                operations.append(APISchema(
                    api_type=APIType.GRAPHQL,
                    endpoint=field["name"],
                    method=op_type.lower(),
                    operation_name=field["name"],
                    summary=field.get("description", ""),
                    parameters=field.get("args", []),
                    required_fields=[a["name"] for a in field.get("args", []) if a.get("required")],
                    response_codes=[],
                ))

        if not operations:
            logger.warning("No Query/Mutation/Subscription types found in SDL")
        logger.info("GraphQL SDL parsed: %d operations", len(operations))
        return operations

    # -- Introspection JSON parsing ---------------------------------------------

    def _from_introspection(self, data: Dict[str, Any]) -> List[APISchema]:
        schema = (data.get("data") or data).get("__schema", {})
        if not schema:
            raise GraphQLParseError("No __schema in introspection response")

        query_type = (schema.get("queryType") or {}).get("name", "Query")
        mutation_type = (schema.get("mutationType") or {}).get("name", "Mutation")
        subscription_type = (schema.get("subscriptionType") or {}).get("name", "Subscription")
        type_map = {t["name"]: t for t in schema.get("types", []) if t.get("kind") == "OBJECT"}

        operations: List[APISchema] = []
        for type_name, op_method in [
            (query_type, "query"),
            (mutation_type, "mutation"),
            (subscription_type, "subscription"),
        ]:
            type_def = type_map.get(type_name)
            if not type_def:
                continue
            for field in type_def.get("fields") or []:
                args = [
                    {
                        "name": a["name"],
                        "type": _unwrap_type(a["type"]),
                        "required": "!" in _unwrap_type(a["type"]),
                    }
                    for a in (field.get("args") or [])
                ]
                operations.append(APISchema(
                    api_type=APIType.GRAPHQL,
                    endpoint=field["name"],
                    method=op_method,
                    operation_name=field["name"],
                    summary=field.get("description") or "",
                    parameters=args,
                    required_fields=[a["name"] for a in args if a.get("required")],
                    response_codes=[],
                ))

        logger.info("GraphQL introspection parsed: %d operations", len(operations))
        return operations


# -- helpers -------------------------------------------------------------------

def _extract_type_block(sdl: str, type_name: str) -> Optional[str]:
    """Return the body of `type <type_name> { ... }` (handles nested braces)."""
    pattern = re.compile(
        r'\btype\s+' + re.escape(type_name) + r'\b[^{]*\{',
        re.IGNORECASE,
    )
    m = pattern.search(sdl)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(sdl) and depth > 0:
        if sdl[i] == "{":
            depth += 1
        elif sdl[i] == "}":
            depth -= 1
        i += 1
    return sdl[start: i - 1]


def _parse_fields(block: str) -> List[Dict[str, Any]]:
    """Parse field definitions from a type body."""
    fields = []
    # Match: fieldName(args): ReturnType
    field_re = re.compile(
        r'(\w+)\s*(?:\(([^)]*)\))?\s*:\s*([\w!\[\] ]+)',
    )
    for m in field_re.finditer(block):
        name = m.group(1)
        args_str = m.group(2) or ""
        args = _parse_args(args_str)
        fields.append({"name": name, "args": args})
    return fields


def _parse_args(args_str: str) -> List[Dict[str, Any]]:
    result = []
    for m in re.finditer(r'(\w+)\s*:\s*([\w!\[\]]+)(?:\s*=\s*\S+)?', args_str):
        type_str = m.group(2)
        result.append({
            "name": m.group(1),
            "type": type_str,
            "required": type_str.endswith("!"),
        })
    return result


def _unwrap_type(type_node: Optional[Dict]) -> str:
    if not type_node:
        return "Unknown"
    name = type_node.get("name")
    if name:
        return name
    inner = type_node.get("ofType")
    if inner:
        return _unwrap_type(inner) + "!"
    return "Unknown"
