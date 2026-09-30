"""Robot Framework templates and QA standards for script generation."""

from __future__ import annotations

from textwrap import dedent

from app.models.schemas import APIType


# ---------------------------------------------------------------------------
# resources.robot  — shared setup file (generated once per ticket)
# ---------------------------------------------------------------------------

def build_robot_resources(
    ticket_id: str,
    base_url: str,
    api_key_default: str = "",
    api_type: APIType = APIType.REST,
) -> str:
    """Return a resources.robot file with shared keywords and variables."""
    if api_type == APIType.GRAPHQL:
        return _robot_resources_graphql(ticket_id, base_url)
    if api_type == APIType.SOAP:
        return _robot_resources_soap(ticket_id, base_url)
    # REST (default) — also used as base for GRPC/WS which lack Robot libraries
    return _robot_resources_rest(ticket_id, base_url, api_key_default)


def _robot_resources_rest(ticket_id: str, base_url: str, api_key_default: str) -> str:
    key_val = api_key_default or ""
    return dedent(
        f"""\
        *** Settings ***
        Library    RequestsLibrary
        Library    Collections
        Library    String

        *** Variables ***
        ${{BASE_URL}}    %{{API_BASE_URL}}    # override with environment variable or default below
        ${{DEFAULT_BASE_URL}}    {base_url}
        ${{API_KEY}}    %{{API_KEY}}    # {key_val}
        ${{TIMEOUT}}    30

        *** Keywords ***
        Create API Session
            [Documentation]    Create a shared HTTP session for {ticket_id} tests.
            ${{headers}}=    Create Dictionary    Content-Type=application/json
            Run Keyword If    '${{API_KEY}}' != ''
            ...    Set To Dictionary    ${{headers}}    Authorization=Bearer ${{API_KEY}}
            ${{base}}=    Get Variable Value    ${{BASE_URL}}    ${{DEFAULT_BASE_URL}}
            Create Session    api    ${{base}}    headers=${{headers}}    timeout=${{TIMEOUT}}

        GET Request
            [Arguments]    ${{endpoint}}    ${{expected_status}}=200
            ${{resp}}=    GET On Session    api    ${{endpoint}}    expected_status=${{expected_status}}
            RETURN    ${{resp}}

        POST Request
            [Arguments]    ${{endpoint}}    ${{body}}    ${{expected_status}}=201
            ${{resp}}=    POST On Session    api    ${{endpoint}}    json=${{body}}    expected_status=${{expected_status}}
            RETURN    ${{resp}}

        PUT Request
            [Arguments]    ${{endpoint}}    ${{body}}    ${{expected_status}}=200
            ${{resp}}=    PUT On Session    api    ${{endpoint}}    json=${{body}}    expected_status=${{expected_status}}
            RETURN    ${{resp}}

        DELETE Request
            [Arguments]    ${{endpoint}}    ${{expected_status}}=204
            ${{resp}}=    DELETE On Session    api    ${{endpoint}}    expected_status=${{expected_status}}
            RETURN    ${{resp}}

        Response Should Contain Key
            [Arguments]    ${{resp}}    ${{key}}
            ${{body}}=    Set Variable    ${{resp.json()}}
            Dictionary Should Contain Key    ${{body}}    ${{key}}

        Response Status Should Be
            [Arguments]    ${{resp}}    ${{status}}
            Should Be Equal As Integers    ${{resp.status_code}}    ${{status}}
        """
    )


def _robot_resources_graphql(ticket_id: str, graphql_url: str) -> str:
    return dedent(
        f"""\
        *** Settings ***
        Library    RequestsLibrary
        Library    Collections
        Library    String

        *** Variables ***
        ${{GRAPHQL_URL}}    %{{GRAPHQL_URL}}
        ${{DEFAULT_GQL_URL}}    {graphql_url}
        ${{API_KEY}}    %{{API_KEY}}
        ${{TIMEOUT}}    30

        *** Keywords ***
        Create GraphQL Session
            [Documentation]    Create HTTP session for {ticket_id} GraphQL tests.
            ${{headers}}=    Create Dictionary    Content-Type=application/json
            Run Keyword If    '${{API_KEY}}' != ''
            ...    Set To Dictionary    ${{headers}}    Authorization=Bearer ${{API_KEY}}
            ${{url}}=    Get Variable Value    ${{GRAPHQL_URL}}    ${{DEFAULT_GQL_URL}}
            Create Session    gql    ${{url}}    headers=${{headers}}    timeout=${{TIMEOUT}}

        Execute GraphQL Query
            [Arguments]    ${{query}}    ${{variables}}=${{None}}
            ${{body}}=    Create Dictionary    query=${{query}}
            Run Keyword If    ${{variables}} is not None
            ...    Set To Dictionary    ${{body}}    variables=${{variables}}
            ${{resp}}=    POST On Session    gql    /    json=${{body}}
            RETURN    ${{resp}}

        Response Should Have No Errors
            [Arguments]    ${{resp}}
            ${{body}}=    Set Variable    ${{resp.json()}}
            Dictionary Should Not Contain Key    ${{body}}    errors

        Response Should Have Errors
            [Arguments]    ${{resp}}
            ${{body}}=    Set Variable    ${{resp.json()}}
            Dictionary Should Contain Key    ${{body}}    errors
        """
    )


def _robot_resources_soap(ticket_id: str, wsdl_url: str) -> str:
    return dedent(
        f"""\
        *** Settings ***
        Library    RequestsLibrary
        Library    Collections
        Library    XML

        *** Variables ***
        ${{WSDL_URL}}    %{{WSDL_URL}}    # {wsdl_url}
        ${{TIMEOUT}}    30

        *** Keywords ***
        Create SOAP Session
            [Documentation]    Placeholder — use Python's zeep library for full SOAP support.
            ...    Run: pip install zeep
            Log    WSDL URL: ${{WSDL_URL}}

        SOAP Request Should Succeed
            [Arguments]    ${{resp_text}}
            Should Not Contain    ${{resp_text}}    Fault
        """
    )


# ---------------------------------------------------------------------------
# QA Standards prompts — one per API type
# ---------------------------------------------------------------------------

_REST_ROBOT_STANDARDS = """Internal QA automation standards for Robot Framework + REST APIs (MUST follow exactly):
- Generate a single valid Robot Framework .robot file.
- Use these sections in order: *** Settings ***, *** Variables ***, *** Test Cases ***, *** Keywords ***.
- *** Settings *** must include: Resource    resources.robot  and  Suite Setup    Create API Session
- Each test case corresponds to one scenario; name it clearly (Title Case, descriptive).
- Apply tags matching scenario type: [Tags]    positive / negative / edge
- Use custom keywords from resources.robot: GET Request, POST Request, PUT Request, DELETE Request.
- Build full paths as: ${BASE_URL}/your/endpoint — do NOT hardcode the base URL.
- Assert status codes using: Response Status Should Be    ${resp}    200
- Assert response body keys using: Response Should Contain Key    ${resp}    key_name
- For negative tests expecting HTTP error codes, pass expected_status to the keyword.
- Add a [Documentation] line to each test case with the ticket ID and scenario description.
- Do NOT redefine keywords from resources.robot.
- Output ONLY valid Robot Framework source. No markdown fences, no Python, no explanation."""

_GRAPHQL_ROBOT_STANDARDS = """Internal QA automation standards for Robot Framework + GraphQL (MUST follow exactly):
- Generate a single valid Robot Framework .robot file.
- Sections in order: *** Settings ***, *** Variables ***, *** Test Cases ***, *** Keywords ***.
- *** Settings *** must include: Resource    resources.robot  and  Suite Setup    Create GraphQL Session
- Each test case corresponds to one scenario.
- Apply tags: [Tags]    positive / negative / edge
- Use Execute GraphQL Query keyword; store the response in ${resp}.
- Positive tests: call Response Should Have No Errors    ${resp} then check ${resp.json()}[data].
- Negative tests: call Response Should Have Errors    ${resp}.
- Write queries as multi-line strings using Robot's ... continuation.
- Add [Documentation] line per test case.
- Output ONLY valid Robot Framework source. No markdown fences."""

_SOAP_ROBOT_STANDARDS = """Internal QA automation standards for Robot Framework + SOAP (MUST follow exactly):
- Generate a single valid Robot Framework .robot file.
- Sections: *** Settings ***, *** Variables ***, *** Test Cases ***, *** Keywords ***.
- *** Settings *** must include: Resource    resources.robot  and  Library    RequestsLibrary
- Each test case corresponds to one SOAP scenario.
- Apply tags: [Tags]    positive / negative / edge
- Build raw SOAP XML envelopes as multi-line strings with ... continuation.
- POST the envelope using: POST On Session    api    ${endpoint}    data=${envelope}    headers=${soap_headers}
- Positive tests: assert status 200 and check response text for expected elements.
- Negative tests: assert response body contains Fault element.
- Add [Documentation] per test case.
- Output ONLY valid Robot Framework source. No markdown fences."""

ROBOT_STANDARDS_BY_TYPE: dict[APIType, str] = {
    APIType.REST: _REST_ROBOT_STANDARDS,
    APIType.GRAPHQL: _GRAPHQL_ROBOT_STANDARDS,
    APIType.SOAP: _SOAP_ROBOT_STANDARDS,
    APIType.GRPC: _REST_ROBOT_STANDARDS,       # fallback — Robot has no gRPC library
    APIType.WEBSOCKET: _REST_ROBOT_STANDARDS,  # fallback — Robot has no native WS library
}


def validate_robot_source(source: str) -> None:
    """Raise ValueError if the source lacks required Robot Framework sections."""
    if "*** Test Cases ***" not in source:
        raise ValueError("Generated Robot source is missing '*** Test Cases ***' section")
