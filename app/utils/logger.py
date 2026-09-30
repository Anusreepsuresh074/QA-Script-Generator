"""
Centralized logging configuration.

Call ``setup_logging()`` once at application startup.  Every module
should obtain its logger via ``logging.getLogger(__name__)``.
"""

from __future__ import annotations

import logging
import sys


_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
)


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger with a unified format."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
