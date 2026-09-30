"""
Orchestrates the full Input & Data Processing pipeline:

  Jira ticket → acceptance criteria → spec parse → API mapping → normalised output

Supports REST (Swagger/OpenAPI), GraphQL, SOAP (WSDL), gRPC (proto), and WebSocket (AsyncAPI).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from app.config.settings import Settings, get_settings
from app.jira.jira_client import JiraClient
from app.jira.ticket_processor import TicketProcessor
from app.mapper.api_mapper import APIMapper
from app.models.schemas import APISchema, ApiMatchScore, APIType, ProcessedDataSchema, TicketSchema
from app.parsers.parser_factory import create_parser, detect_api_type, resolve_spec_source
from app.swagger.swagger_parser import SwaggerParser

logger = logging.getLogger(__name__)


class ProcessingService:
    """High-level facade used by the FastAPI endpoint."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        jira_client: Optional[JiraClient] = None,
        ticket_processor: Optional[TicketProcessor] = None,
        swagger_parser: Optional[SwaggerParser] = None,
        api_mapper: Optional[APIMapper] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._jira = jira_client or JiraClient(self._settings)
        self._processor = ticket_processor or TicketProcessor()
        self._swagger = swagger_parser or SwaggerParser(self._settings)
        self._mapper = api_mapper or APIMapper(self._settings)

    def process_ticket(
        self,
        ticket_id: str,
        swagger_source: Optional[Union[str, Dict[str, Any]]] = None,
        api_type: APIType = APIType.REST,
        spec_url: Optional[str] = None,
        spec_content: Optional[str] = None,
    ) -> ProcessedDataSchema:
        """Run the complete pipeline and return normalised data.

        For REST: pass swagger_source (URL or dict) as before.
        For other types: pass api_type + spec_url/spec_content.
        """

        # 1. Fetch & process the Jira ticket
        logger.info("=== Pipeline start for %s (api_type=%s) ===", ticket_id, api_type)
        raw_issue = self._jira.fetch_ticket(ticket_id)
        ticket: TicketSchema = self._processor.extract_ticket_details(raw_issue)
        logger.info("Ticket processed – %d criteria found", len(ticket.acceptance_criteria))

        # 2. Parse the spec (REST uses legacy SwaggerParser; others use factory)
        endpoints: List[APISchema] = []
        resolved_api_type = api_type

        if api_type == APIType.REST and swagger_source:
            logger.info("Parsing REST/OpenAPI spec")
            endpoints = self._swagger.parse(swagger_source)
            logger.info("Parsed %d endpoint(s)", len(endpoints))

        elif api_type != APIType.REST:
            source = spec_url or spec_content
            if source:
                # Auto-detect if the caller didn't explicitly set type (shouldn't happen, but safe)
                if resolved_api_type == APIType.REST:
                    resolved_api_type = detect_api_type(spec_url, spec_content)
                logger.info("Parsing %s spec", resolved_api_type)
                parser = create_parser(resolved_api_type, timeout=self._settings.swagger_request_timeout)
                endpoints = parser.parse(source)
                logger.info("Parsed %d operation(s)", len(endpoints))

        # 3. Map acceptance criteria to APIs
        matched_apis: List[APISchema] = []
        all_apis: List[APISchema] = []
        match_scores: List[ApiMatchScore] = []
        if ticket.acceptance_criteria and endpoints:
            logger.info("Running API mapping")
            mapping = self._mapper.map_with_scores(
                ticket.acceptance_criteria,
                endpoints,
                summary=ticket.summary,
            )
            matched_apis = mapping.matched
            match_scores = mapping.scores
            all_apis = _summarize_endpoint_scores(endpoints, mapping.scores, self._settings.top_p)
            logger.info("Matched %d API(s)", len(matched_apis))
        elif endpoints:
            matched_apis = endpoints
            all_apis = endpoints

        # 4. Build the normalised output
        result = ProcessedDataSchema(
            ticket_id=ticket.ticket_id,
            summary=ticket.summary,
            description=ticket.description,
            acceptance_criteria=ticket.acceptance_criteria,
            api_type=resolved_api_type,
            apis=matched_apis,
            all_apis=all_apis,
            match_scores=match_scores,
        )
        logger.info("=== Pipeline complete for %s ===", ticket_id)
        return result


def _summarize_endpoint_scores(
    endpoints: List[APISchema],
    scores: List[ApiMatchScore],
    top_p: float,
) -> List[APISchema]:
    """Attach each endpoint's best match score across all criteria."""
    threshold_pct = round(top_p * 100, 1)
    best_by_key: dict[str, float] = {}
    for row in scores:
        key = f"{row.method.upper()} {row.endpoint}"
        best_by_key[key] = max(best_by_key.get(key, 0.0), row.score)

    summarized: List[APISchema] = []
    for ep in endpoints:
        key = f"{ep.method.upper()} {ep.endpoint}"
        best = best_by_key.get(key, 0.0)
        summarized.append(
            ep.model_copy(
                update={
                    "match_score": round(best, 1),
                    "matched": best >= threshold_pct,
                }
            )
        )
    return sorted(summarized, key=lambda item: item.match_score or 0.0, reverse=True)
