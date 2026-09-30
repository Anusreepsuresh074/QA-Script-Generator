"""
Parse SOAP WSDL files into APISchema objects.

Accepts:
  - URL to a WSDL file
  - Raw WSDL XML as a string
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Union
from urllib.parse import urljoin

import requests

from app.models.schemas import APISchema, APIType
from app.parsers.base_parser import BaseSpecParser

logger = logging.getLogger(__name__)

# Common WSDL/SOAP namespace prefixes
_NS = {
    "wsdl": "http://schemas.xmlsoap.org/wsdl/",
    "soap": "http://schemas.xmlsoap.org/wsdl/soap/",
    "soap12": "http://schemas.xmlsoap.org/wsdl/soap12/",
    "xsd": "http://www.w3.org/2001/XMLSchema",
}


class WSDLParseError(Exception):
    pass


class WSDLParser(BaseSpecParser):
    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def parse(self, source: Union[str, Dict[str, Any]]) -> List[APISchema]:
        if isinstance(source, dict):
            raise WSDLParseError("WSDL must be supplied as a URL or XML string, not a dict")

        text: str = source
        if text.startswith(("http://", "https://")):
            return self._from_url(text)
        return self._from_xml(text, service_base_url="")

    def _from_url(self, url: str) -> List[APISchema]:
        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
        except Exception as exc:
            raise WSDLParseError(f"Cannot fetch WSDL from {url}: {exc}") from exc
        return self._from_xml(resp.text, service_base_url=url)

    def _from_xml(self, xml_text: str, service_base_url: str) -> List[APISchema]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise WSDLParseError(f"Invalid WSDL XML: {exc}") from exc

        ns = _detect_wsdl_ns(root)

        # --- Collect service endpoint URLs from <soap:address> ---
        service_urls: Dict[str, str] = {}  # portName -> URL
        for port in root.iter(f"{{{ns['wsdl']}}}port"):
            port_name = port.get("name", "")
            for child in port:
                location = child.get("location", "")
                if location:
                    service_urls[port_name] = location
                    break

        default_url = next(iter(service_urls.values()), service_base_url)

        # --- Collect portType operations (the logical interface) ---
        port_type_ops: Dict[str, Dict[str, Any]] = {}  # opName -> {input_msg, output_msg}
        for port_type in root.iter(f"{{{ns['wsdl']}}}portType"):
            service_name = port_type.get("name", "")
            for op in port_type.iter(f"{{{ns['wsdl']}}}operation"):
                op_name = op.get("name", "")
                if not op_name:
                    continue
                input_msg = output_msg = ""
                for child in op:
                    local = child.tag.split("}")[-1]
                    msg = child.get("message", "").split(":")[-1]
                    if local == "input":
                        input_msg = msg
                    elif local == "output":
                        output_msg = msg
                port_type_ops[op_name] = {
                    "service_name": service_name,
                    "input_message": input_msg,
                    "output_message": output_msg,
                }

        # --- Collect message element types ---
        message_parts: Dict[str, List[str]] = {}  # messageName -> [element/type names]
        for msg in root.iter(f"{{{ns['wsdl']}}}message"):
            msg_name = msg.get("name", "")
            parts = []
            for part in msg.iter(f"{{{ns['wsdl']}}}part"):
                element = part.get("element", part.get("type", "")).split(":")[-1]
                if element:
                    parts.append(element)
            message_parts[msg_name] = parts

        if not port_type_ops:
            logger.warning("No portType operations found in WSDL")

        operations: List[APISchema] = []
        for op_name, op_info in port_type_ops.items():
            input_msg = op_info.get("input_message", "")
            req_fields = message_parts.get(input_msg, [])
            operations.append(APISchema(
                api_type=APIType.SOAP,
                endpoint=default_url,
                method="POST",
                operation_name=op_name,
                service_name=op_info.get("service_name", ""),
                summary=f"SOAP operation: {op_name}",
                parameters=[],
                request_body={"input_message": input_msg, "elements": req_fields},
                required_fields=req_fields,
                response_codes=[],
            ))

        logger.info("WSDL parsed: %d operations", len(operations))
        return operations


def _detect_wsdl_ns(root: ET.Element) -> Dict[str, str]:
    """Use default WSDL namespace but fall back gracefully."""
    tag = root.tag
    if tag.startswith("{"):
        ns_uri = tag[1: tag.index("}")]
        # Could be WSDL 1.1 or 2.0
        return {**_NS, "wsdl": ns_uri}
    return _NS
