"""
Parse AsyncAPI specs (v2.x / v3.x) into APISchema objects for WebSocket testing.

Accepts:
  - URL to an AsyncAPI YAML/JSON file
  - Raw AsyncAPI YAML or JSON text
  - Parsed dict
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Union

import requests
import yaml

from app.models.schemas import APISchema, APIType
from app.parsers.base_parser import BaseSpecParser

logger = logging.getLogger(__name__)


class AsyncAPIParseError(Exception):
    pass


class AsyncAPIParser(BaseSpecParser):
    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def parse(self, source: Union[str, Dict[str, Any]]) -> List[APISchema]:
        if isinstance(source, dict):
            return self._from_dict(source)

        text: str = source
        if text.startswith(("http://", "https://")):
            return self._from_url(text)

        # inline text — try JSON then YAML
        stripped = text.strip()
        try:
            data = json.loads(stripped) if stripped.startswith("{") else yaml.safe_load(stripped)
            if isinstance(data, dict):
                return self._from_dict(data)
        except Exception as exc:
            raise AsyncAPIParseError(f"Cannot parse AsyncAPI spec: {exc}") from exc

        raise AsyncAPIParseError("AsyncAPI spec must be JSON or YAML")

    def _from_url(self, url: str) -> List[APISchema]:
        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
        except Exception as exc:
            raise AsyncAPIParseError(f"Cannot fetch AsyncAPI spec from {url}: {exc}") from exc

        content_type = resp.headers.get("Content-Type", "")
        try:
            if "yaml" in content_type or url.endswith((".yaml", ".yml")):
                data = yaml.safe_load(resp.text)
            else:
                data = resp.json()
        except Exception as exc:
            raise AsyncAPIParseError(f"Cannot decode AsyncAPI response: {exc}") from exc

        return self._from_dict(data)

    def _from_dict(self, spec: Dict[str, Any]) -> List[APISchema]:
        version_str = str(spec.get("asyncapi", "2"))
        major = int(version_str.split(".")[0]) if version_str else 2

        if major >= 3:
            return self._parse_v3(spec)
        return self._parse_v2(spec)

    # -- AsyncAPI 2.x ----------------------------------------------------------

    def _parse_v2(self, spec: Dict[str, Any]) -> List[APISchema]:
        operations: List[APISchema] = []
        channels: Dict[str, Any] = spec.get("channels", {})
        servers = spec.get("servers", {})
        default_url = _first_server_url(servers)

        for channel_path, channel_obj in channels.items():
            if not isinstance(channel_obj, dict):
                continue
            for direction in ("subscribe", "publish"):
                op = channel_obj.get(direction)
                if not op:
                    continue
                messages = _collect_messages_v2(op)
                for msg_name, payload in messages:
                    required = list((payload.get("properties") or {}).keys())[:5] if isinstance(payload, dict) else []
                    operations.append(APISchema(
                        api_type=APIType.WEBSOCKET,
                        endpoint=channel_path,
                        method=direction,
                        channel=channel_path,
                        message_type=msg_name,
                        operation_name=msg_name or channel_path.lstrip("/"),
                        summary=op.get("summary") or op.get("description") or f"WebSocket {direction} on {channel_path}",
                        parameters=[{"name": "url", "type": "string", "required": True, "value": f"{default_url}{channel_path}"}],
                        request_body=payload if direction == "publish" else None,
                        required_fields=required,
                        response_codes=[],
                    ))

        logger.info("AsyncAPI v2 parsed: %d channel operations", len(operations))
        return operations

    # -- AsyncAPI 3.x ----------------------------------------------------------

    def _parse_v3(self, spec: Dict[str, Any]) -> List[APISchema]:
        operations: List[APISchema] = []
        channels: Dict[str, Any] = spec.get("channels", {})
        top_ops: Dict[str, Any] = spec.get("operations", {})
        servers = spec.get("servers", {})
        default_url = _first_server_url(servers)

        for op_id, op_obj in top_ops.items():
            if not isinstance(op_obj, dict):
                continue
            action = op_obj.get("action", "send")  # send | receive
            channel_ref = op_obj.get("channel", {})
            channel_path = channel_ref.get("$ref", "").split("/")[-1] if isinstance(channel_ref, dict) else str(channel_ref)
            messages = _collect_messages_v3(op_obj)

            for msg_name, payload in messages:
                required = list((payload.get("properties") or {}).keys())[:5] if isinstance(payload, dict) else []
                operations.append(APISchema(
                    api_type=APIType.WEBSOCKET,
                    endpoint=channel_path,
                    method=action,
                    channel=channel_path,
                    message_type=msg_name,
                    operation_name=op_id,
                    summary=op_obj.get("summary") or f"WebSocket {action}: {op_id}",
                    parameters=[{"name": "url", "type": "string", "required": True, "value": f"{default_url}{channel_path}"}],
                    request_body=payload if action == "send" else None,
                    required_fields=required,
                    response_codes=[],
                ))

        logger.info("AsyncAPI v3 parsed: %d operations", len(operations))
        return operations


# -- helpers -------------------------------------------------------------------

def _first_server_url(servers: Dict[str, Any]) -> str:
    for server in servers.values():
        if isinstance(server, dict):
            url = server.get("url", "")
            protocol = server.get("protocol", "ws")
            if url and not url.startswith(("ws", "http")):
                return f"{protocol}://{url}"
            return url
    return "ws://localhost"


def _collect_messages_v2(op: Dict[str, Any]) -> List[tuple]:
    """Return [(msg_name, payload_schema)] from a v2 operation."""
    msg = op.get("message", {})
    if "oneOf" in msg:
        return [(m.get("name", m.get("title", "")), m.get("payload", {})) for m in msg["oneOf"]]
    name = msg.get("name") or msg.get("title") or ""
    return [(name, msg.get("payload", {}))]


def _collect_messages_v3(op: Dict[str, Any]) -> List[tuple]:
    """Return [(msg_name, payload_schema)] from a v3 operation."""
    msgs = op.get("messages", [])
    result = []
    for ref_or_msg in msgs:
        if isinstance(ref_or_msg, dict):
            name = ref_or_msg.get("name") or ref_or_msg.get("$ref", "").split("/")[-1]
            payload = ref_or_msg.get("payload", {})
            result.append((name, payload))
    return result or [("", {})]
