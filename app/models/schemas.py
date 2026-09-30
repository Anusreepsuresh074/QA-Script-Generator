"""
Pydantic schemas used across the application.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# API Type
# ---------------------------------------------------------------------------

class APIType(str, Enum):
    REST = "rest"
    GRAPHQL = "graphql"
    SOAP = "soap"
    GRPC = "grpc"
    WEBSOCKET = "websocket"


class TestFramework(str, Enum):
    PYTEST = "pytest"        # Python – pytest + requests/zeep/grpcio/websockets
    ROBOT = "robot"          # Robot Framework – keyword-driven .robot files
    JEST = "jest"            # JavaScript – Jest + axios/supertest
    POSTMAN = "postman"      # Postman Collection v2.1 JSON (run with Newman)


# ---------------------------------------------------------------------------
# API Schema
# ---------------------------------------------------------------------------

class APISchema(BaseModel):
    """Represents a single parsed API operation (REST endpoint, GraphQL op, SOAP op, gRPC method, or WS channel)."""

    api_type: APIType = Field(APIType.REST, description="Protocol type of this operation")

    # Primary identifier fields — used by the mapper as the composite key
    endpoint: str = Field("", description="URL path (REST), operation name (GraphQL), service.method (gRPC), channel (WS)")
    method: str = Field("", description="HTTP method (REST), operation type (GraphQL query/mutation/subscription), rpc type (gRPC), or direction (WS send/receive)")

    summary: Optional[str] = Field(None, description="Short description from the spec")
    parameters: List[Dict[str, Any]] = Field(default_factory=list, description="Path/query params (REST) or arguments (GraphQL/gRPC)")
    request_body: Optional[Dict[str, Any]] = Field(None, description="Request body / input message schema")
    required_fields: List[str] = Field(default_factory=list, description="Required fields for the request")
    response_codes: List[str] = Field(default_factory=list, description="HTTP response codes (REST) or error codes")
    authentication_type: Optional[str] = Field(None, description="Auth mechanism if specified")
    examples: Optional[Dict[str, Any]] = Field(None, description="Example payloads from the spec")
    match_score: Optional[float] = Field(None, description="Relevance score (0–100%) from criteria matching")
    matched: Optional[bool] = Field(None, description="True when score meets TOP_P threshold")

    # GraphQL / SOAP / gRPC — named operation/method
    operation_name: Optional[str] = Field(None, description="GraphQL operation name, SOAP operation, or gRPC method name")

    # SOAP / gRPC — service identifier
    service_name: Optional[str] = Field(None, description="SOAP service name or gRPC service name")

    # gRPC — RPC streaming classification
    rpc_type: Optional[str] = Field(None, description="unary | server_streaming | client_streaming | bidi_streaming")

    # WebSocket — channel and message event
    channel: Optional[str] = Field(None, description="WebSocket channel path")
    message_type: Optional[str] = Field(None, description="WebSocket message/event type name")


class ApiMatchScore(BaseModel):
    """Match probability for one criterion against one API endpoint."""

    criterion: str
    method: str
    endpoint: str
    score: float = Field(..., description="Match probability 0–100")
    selected: bool = Field(
        False,
        description="True when this is the best match for the criterion and meets TOP_P",
    )


# ---------------------------------------------------------------------------
# Acceptance Criteria Schema
# ---------------------------------------------------------------------------

class AcceptanceCriteriaSchema(BaseModel):
    """Container for extracted acceptance criteria."""

    acceptance_criteria: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Ticket Schema
# ---------------------------------------------------------------------------

class TicketSchema(BaseModel):
    """Normalized representation of a Jira ticket."""

    ticket_id: str
    summary: str = ""
    description: Optional[str] = None
    acceptance_criteria: List[str] = Field(default_factory=list)
    labels: List[str] = Field(default_factory=list)
    linked_issues: List[str] = Field(default_factory=list)
    comments: List[str] = Field(default_factory=list)
    priority: Optional[str] = None
    status: Optional[str] = None
    attachments: List[str] = Field(default_factory=list)
    epic: Optional[str] = None


# ---------------------------------------------------------------------------
# Processed Data Schema (normalized output)
# ---------------------------------------------------------------------------

class ProcessedDataSchema(BaseModel):
    """
    Unified data package passed to downstream LLM processing.
    """

    ticket_id: str
    summary: str
    description: Optional[str] = None
    acceptance_criteria: List[str] = Field(default_factory=list)
    api_type: APIType = Field(APIType.REST, description="API protocol detected or specified for this ticket")
    apis: List[APISchema] = Field(
        default_factory=list,
        description="Endpoints that passed TOP_P threshold",
    )
    all_apis: List[APISchema] = Field(
        default_factory=list,
        description="All parsed endpoints with best match score per endpoint",
    )
    match_scores: List[ApiMatchScore] = Field(
        default_factory=list,
        description="Per-criterion match probability for each endpoint",
    )


# ---------------------------------------------------------------------------
# Request / Response schemas for the FastAPI endpoint
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test Scenario Schemas
# ---------------------------------------------------------------------------

class TestScenarioType(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    EDGE = "edge"


class TestScenario(BaseModel):
    """A single generated test scenario."""

    name: str = Field(..., description="Short scenario title")
    type: TestScenarioType = Field(..., description="positive, negative, or edge")
    description: str = Field(..., description="What this scenario validates")
    steps: List[str] = Field(default_factory=list, description="Step-by-step test actions")
    expected_result: str = Field(..., description="Expected outcome of the test")
    api_endpoint: Optional[str] = Field(None, description="API path this scenario targets")
    method: Optional[str] = Field(None, description="HTTP method (GET, POST, …)")


class GeneratedScenariosSchema(BaseModel):
    """LLM-generated test scenarios for a Jira ticket."""

    ticket_id: str
    summary: str
    scenarios: List[TestScenario] = Field(default_factory=list)


class JiraTicketSummary(BaseModel):
    """Key details of the Jira ticket returned after processing."""

    ticket_id: str
    summary: str
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None
    reporter: Optional[str] = None
    comment_url: Optional[str] = None
    attachment_filename: Optional[str] = None


class GenerateScenariosRequest(BaseModel):
    """POST body for /generate-scenarios."""

    ticket_id: str = Field(..., description="Jira ticket key, e.g. PROJ-123")
    api_type: APIType = Field(APIType.REST, description="API protocol: rest | graphql | soap | grpc | websocket")
    swagger_url: Optional[str] = Field(None, description="URL or local path to Swagger/OpenAPI spec (REST)")
    swagger_content: Optional[Dict[str, Any]] = Field(None, description="Inline Swagger/OpenAPI spec dict (REST)")
    spec_url: Optional[str] = Field(None, description="URL to spec file (GraphQL SDL, WSDL, .proto, AsyncAPI)")
    spec_content: Optional[str] = Field(None, description="Raw spec text (SDL, WSDL XML, proto, AsyncAPI YAML/JSON)")


class GenerateScenariosResponse(BaseModel):
    """Response wrapper for /generate-scenarios."""

    success: bool = True
    data: Optional[GeneratedScenariosSchema] = None
    ticket: Optional[JiraTicketSummary] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Request / Response schemas for the FastAPI endpoint
# ---------------------------------------------------------------------------

class ProcessTicketRequest(BaseModel):
    """POST body for /process-ticket."""

    ticket_id: str = Field(..., description="Jira ticket key, e.g. PROJ-123")
    api_type: APIType = Field(APIType.REST, description="API protocol: rest | graphql | soap | grpc | websocket")
    swagger_url: Optional[str] = Field(None, description="URL or local path to Swagger/OpenAPI spec (REST)")
    swagger_content: Optional[Dict[str, Any]] = Field(
        None,
        description="Inline Swagger/OpenAPI spec dict (REST only)",
    )
    spec_url: Optional[str] = Field(None, description="URL to spec file (GraphQL SDL, WSDL, .proto, AsyncAPI)")
    spec_content: Optional[str] = Field(None, description="Raw spec text (SDL, WSDL XML, proto, AsyncAPI YAML/JSON)")


class ProcessTicketResponse(BaseModel):
    """Response wrapper for /process-ticket."""

    success: bool = True
    data: Optional[ProcessedDataSchema] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase 3 – Pytest script generation
# ---------------------------------------------------------------------------

class GeneratedScriptFile(BaseModel):
    """A single generated file on disk."""

    filename: str
    path: str
    content: str


class GeneratedScriptsSchema(BaseModel):
    """Automation scripts generated for a Jira ticket (framework-agnostic)."""

    ticket_id: str
    summary: str
    output_dir: str
    framework: TestFramework = Field(TestFramework.PYTEST, description="Framework used to generate the scripts")
    files: List[GeneratedScriptFile] = Field(default_factory=list)


class GenerateScriptsRequest(BaseModel):
    """POST body for /generate-scripts."""

    ticket_id: str = Field(..., description="Jira ticket key, e.g. PROJ-123")
    api_type: APIType = Field(APIType.REST, description="API protocol: rest | graphql | soap | grpc | websocket")
    framework: TestFramework = Field(TestFramework.PYTEST, description="Test framework: pytest | robot | jest | postman")
    swagger_url: Optional[str] = Field(None, description="URL or local path to Swagger/OpenAPI spec (REST)")
    swagger_content: Optional[Dict[str, Any]] = Field(None, description="Inline Swagger/OpenAPI spec dict (REST)")
    spec_url: Optional[str] = Field(None, description="URL to spec file (GraphQL SDL, WSDL, .proto, AsyncAPI)")
    spec_content: Optional[str] = Field(None, description="Raw spec text (SDL, WSDL XML, proto, AsyncAPI YAML/JSON)")
    api_base_url: Optional[str] = Field(None, description="Override API base URL for generated tests")
    attach_to_jira: bool = Field(True, description="Attach generated scripts and post summary to Jira")
    create_pr: bool = Field(
        False,
        description=(
            "After scripts are generated, run tests and open a PR in the automation "
            "repository when at least one test passes (full suite green is not required)"
        ),
    )


class GenerateScriptsResponse(BaseModel):
    """Response wrapper for /generate-scripts."""

    success: bool = True
    data: Optional[GeneratedScriptsSchema] = None
    scenarios: Optional[GeneratedScenariosSchema] = None
    ticket: Optional[JiraTicketSummary] = None
    error: Optional[str] = None
