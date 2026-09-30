"""
FastAPI application entry-point.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.ai.llm_client import LLMTransientError
from app.config.settings import get_settings
from app.jira.jira_client import (
    JiraAuthenticationError,
    JiraConnectionError,
    JiraTicketNotFoundError,
)
from app.jira.jira_client import JiraClient
from app.models.schemas import (
    APIType,
    GenerateScenariosRequest,
    GenerateScenariosResponse,
    GenerateScriptsRequest,
    GenerateScriptsResponse,
    JiraTicketSummary,
    ProcessedDataSchema,
    ProcessTicketRequest,
    ProcessTicketResponse,
)
from app.services.scenario_generator import ScenarioGenerationError, ScenarioGenerator
from app.services.script_generator import ScriptGenerationError, ScriptGenerator
from app.services.processing_service import ProcessingService
from app.swagger.swagger_parser import SwaggerParseError
from app.utils.csv_builder import build_comment_adf, build_scenarios_csv, build_scripts_comment_adf
from app.utils.logger import setup_logging
from app.routes.dashboard import router as dashboard_router

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("Application starting (log_level=%s)", settings.log_level)
    yield
    logger.info("Application shutting down")


app = FastAPI(
    title="QA Script Generator",
    description=(
        "Processes Jira tickets and Swagger/OpenAPI specs, generates test scenarios, "
        "and produces runnable test scripts (Pytest, Robot Framework, Jest, Postman)."
    ),
    version="1.2.0",
    lifespan=lifespan,
)

app.include_router(dashboard_router)

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    """Serve the workflow dashboard UI."""
    index = _STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(index)


def _get_service() -> ProcessingService:
    return ProcessingService()


def _resolve_swagger_source(body) -> object:
    """Swagger/OpenAPI source for a REST request: explicit body value, else the
    SWAGGER_URL default from .env."""
    swagger_source = body.swagger_url or body.swagger_content
    if not swagger_source and body.api_type == APIType.REST:
        swagger_source = get_settings().swagger_url
    return swagger_source


@app.post(
    "/process-ticket",
    response_model=ProcessTicketResponse,
    summary="Process a Jira ticket and optional Swagger spec",
)
async def process_ticket(body: ProcessTicketRequest) -> ProcessTicketResponse:
    """Fetch a Jira ticket, extract criteria, parse an API spec, and return
    a normalised payload ready for LLM consumption."""

    service = _get_service()

    swagger_source = _resolve_swagger_source(body)

    try:
        result: ProcessedDataSchema = service.process_ticket(
            ticket_id=body.ticket_id,
            swagger_source=swagger_source,
            api_type=body.api_type,
            spec_url=body.spec_url,
            spec_content=body.spec_content,
        )
    except JiraAuthenticationError:
        raise HTTPException(status_code=401, detail="Jira authentication failed")
    except JiraTicketNotFoundError:
        raise HTTPException(status_code=404, detail=f"Ticket {body.ticket_id} not found")
    except JiraConnectionError as exc:
        raise HTTPException(status_code=502, detail=f"Jira unreachable: {exc}")
    except SwaggerParseError as exc:
        raise HTTPException(status_code=422, detail=f"Swagger parse error: {exc}")
    except LLMTransientError as exc:
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}")
    except Exception as exc:
        logger.exception("Unhandled error processing ticket %s", body.ticket_id)
        raise HTTPException(status_code=500, detail=str(exc))

    return ProcessTicketResponse(success=True, data=result)


@app.post(
    "/generate-scenarios",
    response_model=GenerateScenariosResponse,
    summary="Generate positive, negative, and edge test scenarios from a Jira ticket",
)
async def generate_scenarios(body: GenerateScenariosRequest) -> GenerateScenariosResponse:
    """Fetch a Jira ticket, map to API endpoints (optional), then use an LLM to generate
    positive, negative, and edge test scenarios for each acceptance criterion."""

    service = _get_service()
    swagger_source = _resolve_swagger_source(body)

    try:
        processed: ProcessedDataSchema = service.process_ticket(
            ticket_id=body.ticket_id,
            swagger_source=swagger_source,
            api_type=body.api_type,
            spec_url=body.spec_url,
            spec_content=body.spec_content,
        )
    except JiraAuthenticationError:
        raise HTTPException(status_code=401, detail="Jira authentication failed")
    except JiraTicketNotFoundError:
        raise HTTPException(status_code=404, detail=f"Ticket {body.ticket_id} not found")
    except JiraConnectionError as exc:
        raise HTTPException(status_code=502, detail=f"Jira unreachable: {exc}")
    except SwaggerParseError as exc:
        raise HTTPException(status_code=422, detail=f"Swagger parse error: {exc}")
    except Exception as exc:
        logger.exception("Error processing ticket %s", body.ticket_id)
        raise HTTPException(status_code=500, detail=str(exc))

    try:
        generator = ScenarioGenerator()
        result = generator.generate(processed)
    except ScenarioGenerationError as exc:
        raise HTTPException(status_code=502, detail=f"Scenario generation failed: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error generating scenarios for %s", body.ticket_id)
        raise HTTPException(status_code=500, detail=str(exc))

    # Build CSV and post to Jira
    filename = f"{body.ticket_id}_test_scenarios.csv"
    csv_bytes = build_scenarios_csv(result)
    adf_comment = build_comment_adf(result, filename)

    jira = JiraClient()
    comment_url: str | None = None
    attachment_filename: str | None = None

    try:
        comment_resp = jira.add_comment(body.ticket_id, adf_comment)
        comment_url = comment_resp.get("self")
        logger.info("Comment posted to %s", body.ticket_id)
    except Exception as exc:
        logger.warning("Could not post comment to %s: %s", body.ticket_id, exc)

    try:
        jira.add_attachment(body.ticket_id, filename, csv_bytes)
        attachment_filename = filename
        logger.info("Attachment %s added to %s", filename, body.ticket_id)
    except Exception as exc:
        logger.warning("Could not attach CSV to %s: %s", body.ticket_id, exc)

    # Fetch updated ticket for response
    raw = jira.fetch_ticket(body.ticket_id)
    fields = raw.get("fields", {})
    ticket_summary = JiraTicketSummary(
        ticket_id=body.ticket_id,
        summary=fields.get("summary", ""),
        status=(fields.get("status") or {}).get("name"),
        priority=(fields.get("priority") or {}).get("name"),
        assignee=(fields.get("assignee") or {}).get("displayName"),
        reporter=(fields.get("reporter") or {}).get("displayName"),
        comment_url=comment_url,
        attachment_filename=attachment_filename,
    )

    return GenerateScenariosResponse(success=True, data=result, ticket=ticket_summary)


@app.post(
    "/generate-scripts",
    response_model=GenerateScriptsResponse,
    summary="Generate Pytest automation scripts from a Jira ticket (Phases 1–3)",
)
async def generate_scripts(body: GenerateScriptsRequest) -> GenerateScriptsResponse:
    """Run the full pipeline: process ticket → generate scenarios → generate Pytest scripts."""

    service = _get_service()
    swagger_source = _resolve_swagger_source(body)

    try:
        processed: ProcessedDataSchema = service.process_ticket(
            ticket_id=body.ticket_id,
            swagger_source=swagger_source,
            api_type=body.api_type,
            spec_url=body.spec_url,
            spec_content=body.spec_content,
        )
    except JiraAuthenticationError:
        raise HTTPException(status_code=401, detail="Jira authentication failed")
    except JiraTicketNotFoundError:
        raise HTTPException(status_code=404, detail=f"Ticket {body.ticket_id} not found")
    except JiraConnectionError as exc:
        raise HTTPException(status_code=502, detail=f"Jira unreachable: {exc}")
    except SwaggerParseError as exc:
        raise HTTPException(status_code=422, detail=f"Swagger parse error: {exc}")
    except Exception as exc:
        logger.exception("Error processing ticket %s", body.ticket_id)
        raise HTTPException(status_code=500, detail=str(exc))

    try:
        scenarios = ScenarioGenerator().generate(processed)
    except ScenarioGenerationError as exc:
        raise HTTPException(status_code=502, detail=f"Scenario generation failed: {exc}")

    try:
        scripts = ScriptGenerator().generate(
            processed,
            scenarios,
            base_url=body.api_base_url,
            framework=body.framework,
        )
    except ScriptGenerationError as exc:
        raise HTTPException(status_code=502, detail=f"Script generation failed: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error generating scripts for %s", body.ticket_id)
        raise HTTPException(status_code=500, detail=str(exc))

    jira = JiraClient()
    comment_url: str | None = None
    attachment_names: list[str] = []

    if body.attach_to_jira:
        try:
            comment_resp = jira.add_comment(body.ticket_id, build_scripts_comment_adf(scripts))
            comment_url = comment_resp.get("self")
        except Exception as exc:
            logger.warning("Could not post script comment to %s: %s", body.ticket_id, exc)

        for script_file in scripts.files:
            try:
                jira.add_attachment(
                    body.ticket_id,
                    script_file.filename,
                    script_file.content.encode("utf-8"),
                )
                attachment_names.append(script_file.filename)
            except Exception as exc:
                logger.warning(
                    "Could not attach %s to %s: %s",
                    script_file.filename,
                    body.ticket_id,
                    exc,
                )

    raw = jira.fetch_ticket(body.ticket_id)
    fields = raw.get("fields", {})
    ticket_summary = JiraTicketSummary(
        ticket_id=body.ticket_id,
        summary=fields.get("summary", ""),
        status=(fields.get("status") or {}).get("name"),
        priority=(fields.get("priority") or {}).get("name"),
        assignee=(fields.get("assignee") or {}).get("displayName"),
        reporter=(fields.get("reporter") or {}).get("displayName"),
        comment_url=comment_url,
        attachment_filename=", ".join(attachment_names) if attachment_names else None,
    )

    return GenerateScriptsResponse(
        success=True,
        data=scripts,
        scenarios=scenarios,
        ticket=ticket_summary,
    )


@app.get("/health", summary="Health check")
async def health() -> dict:
    return {"status": "ok"}
