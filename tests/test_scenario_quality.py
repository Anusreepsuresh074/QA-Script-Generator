"""Quality checks for deterministic Phase 2 scenario filtering."""

from app.models.schemas import (
    APISchema,
    APIType,
    ProcessedDataSchema,
    TestScenario as Scenario,
    TestScenarioType as ScenarioType,
)
from app.services.scenario_generator import (
    _dedupe_criteria,
    _filter_scenarios,
    _is_duplicate_scenario,
)


def _scenario(
    name: str,
    *,
    description: str = "Verify the user list is returned",
    endpoint: str | None = "/users",
    method: str | None = "GET",
    expected: str = "Response is 200 and contains users",
) -> Scenario:
    return Scenario(
        name=name,
        type=ScenarioType.POSITIVE,
        description=description,
        steps=["Send GET /users"],
        expected_result=expected,
        api_endpoint=endpoint,
        method=method,
    )


def _processed() -> ProcessedDataSchema:
    return ProcessedDataSchema(
        ticket_id="QA-1",
        summary="List users",
        acceptance_criteria=["GET /users returns users with status 200"],
        api_type=APIType.REST,
        apis=[
            APISchema(
                endpoint="/users",
                method="GET",
                response_codes=["200"],
                match_score=90.0,
                matched=True,
            )
        ],
    )


def test_deduplicates_equivalent_acceptance_criteria() -> None:
    criteria = _dedupe_criteria(
        [
            "GET users returns 200",
            "GET users returns 200.",
            "Missing user returns 404",
        ]
    )

    assert criteria == ["GET users returns 200", "Missing user returns 404"]


def test_detects_reworded_duplicate_scenarios() -> None:
    first = _scenario("List users successfully")
    duplicate = _scenario("Successfully list users")

    assert _is_duplicate_scenario(duplicate, first)


def test_keeps_valid_and_auto_links_missing_or_unmapped_endpoints() -> None:
    valid = _scenario("List users successfully")
    duplicate = _scenario("Successfully list users")
    missing_endpoint = _scenario(
        "Validate page totals",
        description="Confirm page metadata totals are correct",
        endpoint=None,
        method=None,
        expected="page and total fields match expected values",
    )
    invented_endpoint = _scenario(
        "List products",
        description="Retrieve product catalogue items",
        endpoint="/products",
        expected="products array is returned",
    )
    incomplete = _scenario("Missing expected result", expected="")

    filtered = _filter_scenarios(
        [valid, duplicate, missing_endpoint, invented_endpoint, incomplete],
        _processed(),
    )

    assert len(filtered) == 3
    assert filtered[0].name == "List users successfully"
    assert filtered[1].api_endpoint == "/users"
    assert filtered[1].method == "GET"
    assert filtered[1].name == "Validate page totals"
    assert filtered[2].api_endpoint == "/users"
    assert filtered[2].method == "GET"
    assert filtered[2].name == "List products"


def test_filters_generic_scenario_not_supported_by_ticket() -> None:
    generic = _scenario(
        "Load test users endpoint",
        description="Send high traffic to measure throughput performance",
    )

    assert _filter_scenarios([generic], _processed()) == []
