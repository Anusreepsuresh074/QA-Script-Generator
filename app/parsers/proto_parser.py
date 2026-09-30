"""
Parse gRPC Protocol Buffer (.proto) files into APISchema objects.

Accepts:
  - URL to a .proto file
  - Raw proto file content as a string

Uses regex parsing — no protoc dependency required.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Union

import requests

from app.models.schemas import APISchema, APIType
from app.parsers.base_parser import BaseSpecParser

logger = logging.getLogger(__name__)


class ProtoParseError(Exception):
    pass


class ProtoParser(BaseSpecParser):
    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def parse(self, source: Union[str, Dict[str, Any]]) -> List[APISchema]:
        if isinstance(source, dict):
            raise ProtoParseError("Proto spec must be a URL or raw .proto text, not a dict")

        text: str = source
        if text.startswith(("http://", "https://")):
            return self._from_url(text)
        return self._from_proto(text)

    def _from_url(self, url: str) -> List[APISchema]:
        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
        except Exception as exc:
            raise ProtoParseError(f"Cannot fetch proto file from {url}: {exc}") from exc
        return self._from_proto(resp.text)

    def _from_proto(self, content: str) -> List[APISchema]:
        # Strip line comments
        content = re.sub(r"//[^\n]*", "", content)
        # Strip block comments
        content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)

        operations: List[APISchema] = []
        service_re = re.compile(r"\bservice\s+(\w+)\s*\{", re.IGNORECASE)
        rpc_re = re.compile(
            r"\brpc\s+(\w+)\s*\(\s*(stream\s+)?(\w+)\s*\)\s*returns\s*\(\s*(stream\s+)?(\w+)\s*\)",
            re.IGNORECASE,
        )

        # Walk service blocks (handle nested braces)
        for service_match in service_re.finditer(content):
            service_name = service_match.group(1)
            block_start = service_match.end()
            block = _extract_block(content, block_start)

            for rpc_match in rpc_re.finditer(block):
                method_name = rpc_match.group(1)
                client_stream = bool(rpc_match.group(2))
                request_type = rpc_match.group(3)
                server_stream = bool(rpc_match.group(4))
                response_type = rpc_match.group(5)

                if client_stream and server_stream:
                    rpc_type = "bidi_streaming"
                elif client_stream:
                    rpc_type = "client_streaming"
                elif server_stream:
                    rpc_type = "server_streaming"
                else:
                    rpc_type = "unary"

                # Parse request message fields
                req_fields = _extract_message_fields(content, request_type)

                operations.append(APISchema(
                    api_type=APIType.GRPC,
                    endpoint=f"{service_name}.{method_name}",
                    method=rpc_type,
                    operation_name=method_name,
                    service_name=service_name,
                    rpc_type=rpc_type,
                    summary=f"{rpc_type} RPC: {service_name}.{method_name}({request_type}) → {response_type}",
                    parameters=[{"name": f, "type": "field"} for f in req_fields],
                    request_body={"message": request_type, "fields": req_fields},
                    required_fields=req_fields,
                    response_codes=["OK", "INVALID_ARGUMENT", "NOT_FOUND", "INTERNAL"],
                ))

        if not operations:
            logger.warning("No service/rpc definitions found in proto file")
        logger.info("Proto parsed: %d RPC methods", len(operations))
        return operations


def _extract_block(content: str, start: int) -> str:
    """Extract the content of a `{...}` block starting at `start` index."""
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
        i += 1
    return content[start: i - 1]


def _extract_message_fields(content: str, message_name: str) -> List[str]:
    """Return field names of a proto message."""
    msg_re = re.compile(r"\bmessage\s+" + re.escape(message_name) + r"\s*\{", re.IGNORECASE)
    m = msg_re.search(content)
    if not m:
        return []
    block = _extract_block(content, m.end())
    # Match: [optional|required|repeated] Type fieldName = N;
    field_re = re.compile(r"(?:optional|required|repeated)?\s*\w+\s+(\w+)\s*=\s*\d+")
    return [fm.group(1) for fm in field_re.finditer(block)]
