"""
Factory for creating the right parser based on API type,
with auto-detection from spec URL/content.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Union

from app.models.schemas import APIType
from app.parsers.asyncapi_parser import AsyncAPIParser
from app.parsers.base_parser import BaseSpecParser
from app.parsers.graphql_parser import GraphQLParser
from app.parsers.proto_parser import ProtoParser
from app.parsers.wsdl_parser import WSDLParser

logger = logging.getLogger(__name__)


def detect_api_type(
    spec_url: Optional[str] = None,
    spec_content: Optional[str] = None,
) -> APIType:
    """Guess the API type from the spec URL extension or content markers."""
    if spec_url:
        lower = spec_url.lower().split("?")[0]  # strip query params
        if lower.endswith((".graphql", ".gql")):
            return APIType.GRAPHQL
        if lower.endswith(".wsdl"):
            return APIType.SOAP
        if lower.endswith(".proto"):
            return APIType.GRPC
        if "asyncapi" in lower:
            return APIType.WEBSOCKET

    if spec_content:
        content = spec_content.strip()
        if content.startswith('syntax = "proto') or content.startswith("syntax = 'proto"):
            return APIType.GRPC
        if ("<wsdl:definitions" in content
                or "<definitions" in content
                and "xmlsoap.org/wsdl" in content):
            return APIType.SOAP
        if '"asyncapi"' in content or "asyncapi:" in content:
            return APIType.WEBSOCKET
        if ("type Query" in content
                or "type Mutation" in content
                or "type Subscription" in content):
            return APIType.GRAPHQL

    return APIType.REST


def create_parser(api_type: APIType, timeout: int = 30) -> BaseSpecParser:
    """Return the parser instance for the given API type."""
    if api_type == APIType.GRAPHQL:
        return GraphQLParser(timeout=timeout)
    if api_type == APIType.SOAP:
        return WSDLParser(timeout=timeout)
    if api_type == APIType.GRPC:
        return ProtoParser(timeout=timeout)
    if api_type == APIType.WEBSOCKET:
        return AsyncAPIParser(timeout=timeout)
    # REST falls back to the existing SwaggerParser (imported lazily to avoid circular imports)
    from app.swagger.swagger_parser import SwaggerParser
    from app.config.settings import get_settings
    return SwaggerParser(get_settings())


def resolve_spec_source(
    api_type: APIType,
    swagger_url: Optional[str],
    swagger_content: Optional[Dict[str, Any]],
    spec_url: Optional[str],
    spec_content: Optional[str],
) -> Optional[Union[str, Dict[str, Any]]]:
    """Normalise the spec inputs from a request into a single source value."""
    if api_type == APIType.REST:
        # Prefer the legacy swagger fields for REST
        return swagger_url or swagger_content or spec_url or spec_content
    return spec_url or spec_content or swagger_url
