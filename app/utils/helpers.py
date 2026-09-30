"""
Shared utility helpers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml

logger = logging.getLogger(__name__)


def load_spec_file(path: Union[str, Path]) -> Dict[str, Any]:
    """Load a JSON or YAML file and return its contents as a dict.

    Raises ``ValueError`` when the file extension is unsupported.
    Raises ``FileNotFoundError`` when the path does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Spec file not found: {path}")

    raw = path.read_text(encoding="utf-8")

    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(raw)
    if path.suffix == ".json":
        return json.loads(raw)

    raise ValueError(f"Unsupported file extension: {path.suffix}")


def safe_get(data: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dicts.

    >>> safe_get({"a": {"b": 1}}, "a", "b")
    1
    >>> safe_get(None, "x", default="fallback")
    'fallback'
    """
    current = data
    for k in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(k, default)
    return current


def sanitize_text(text: Optional[str]) -> str:
    """Strip and collapse whitespace; return empty string for None."""
    if not text:
        return ""
    return " ".join(text.split())
