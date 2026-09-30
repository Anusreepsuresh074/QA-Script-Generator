"""Keyword mapping of acceptance criteria to API endpoints."""

import pytest

from app.mapper.api_mapper import APIMapper
from app.swagger.swagger_parser import SwaggerParser
from demo.mock_server import api as customers_api


@pytest.fixture(scope="module")
def endpoints():
    return SwaggerParser().parse(customers_api.openapi())


def _ops(schemas) -> set[tuple[str, str]]:
    return {(s.method, s.endpoint) for s in schemas}


@pytest.mark.positive
def test_create_criteria_map_to_the_create_endpoint(endpoints):
    matched = APIMapper().map(
        ["User can create a customer with name and email via POST /customers"],
        endpoints,
        summary="Create Customer API",
    )

    assert ("POST", "/customers") in _ops(matched)


@pytest.mark.positive
def test_every_endpoint_gets_a_score(endpoints):
    result = APIMapper().map_with_scores(["User can delete a customer"], endpoints)

    assert len(result.scores) == len(endpoints)
    assert all(0 <= s.score <= 100 for s in result.scores)


@pytest.mark.edge
def test_no_endpoints_means_no_matches():
    assert APIMapper().map(["User can create a customer"], []) == []


@pytest.mark.edge
def test_no_criteria_and_no_summary_means_no_matches(endpoints):
    assert APIMapper().map([], endpoints) == []
