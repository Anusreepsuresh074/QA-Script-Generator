"""Abstract base class for all spec parsers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Union, Dict, Any

from app.models.schemas import APISchema


class BaseSpecParser(ABC):
    """All parsers must implement parse() and return a list of APISchema objects."""

    @abstractmethod
    def parse(self, source: Union[str, Dict[str, Any]]) -> List[APISchema]:
        """Parse a spec source (URL string, file path string, raw text string, or dict)
        and return normalised APISchema objects.
        """
