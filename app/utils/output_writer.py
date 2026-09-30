"""Write generated test files to the configured output directory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.config.settings import Settings, get_settings
from app.models.schemas import GeneratedScriptFile, GeneratedScriptsSchema, TestFramework

logger = logging.getLogger(__name__)


def write_scripts(
    ticket_id: str,
    summary: str,
    files: list[tuple[str, str]],
    settings: Optional[Settings] = None,
    framework: TestFramework = TestFramework.PYTEST,
) -> GeneratedScriptsSchema:
    """Persist generated files under ``output/<ticket_id>/`` and return metadata."""
    cfg = settings or get_settings()
    root = Path(cfg.output_dir) / ticket_id
    root.mkdir(parents=True, exist_ok=True)

    written: list[GeneratedScriptFile] = []
    for filename, content in files:
        path = root / filename
        path.write_text(content, encoding="utf-8")
        logger.info("Wrote %s (%d bytes)", str(path), len(content))
        written.append(GeneratedScriptFile(filename=filename, path=str(path), content=content))

    return GeneratedScriptsSchema(
        ticket_id=ticket_id,
        summary=summary,
        output_dir=str(root),
        framework=framework,
        files=written,
    )
