"""Acceptance-criteria extraction from Jira ticket text (no LLM involved)."""

import pytest

from app.jira.criteria_extractor import CriteriaExtractor
from demo.mock_server import TICKETS


@pytest.fixture
def extractor():
    return CriteriaExtractor()


@pytest.mark.positive
def test_extracts_every_bullet_under_the_acceptance_criteria_heading(extractor):
    criteria = extractor.extract(TICKETS["QA-1"]["description"])

    assert criteria == [
        "User can create a customer with name and email via POST /customers",
        "Email is mandatory and must be a valid email address",
        "Name is mandatory and must not be empty",
        "Creating a customer with a duplicate email returns 409 Conflict",
        "A successful create returns 201 with the new customer id",
    ]


@pytest.mark.positive
def test_numbered_criteria_are_extracted(extractor):
    text = "Acceptance Criteria:\n1. Login succeeds with valid credentials\n2) Login fails with a wrong password"

    assert extractor.extract(text) == [
        "Login succeeds with valid credentials",
        "Login fails with a wrong password",
    ]


@pytest.mark.positive
def test_sub_headings_are_kept_as_a_prefix(extractor):
    text = (
        "Acceptance criteria:\n"
        "Create User:\n"
        "- Name is required for a new user\n"
        "Delete User:\n"
        "- Deleting an unknown user returns 404\n"
    )

    assert extractor.extract(text) == [
        "Create User – Name is required for a new user",
        "Delete User – Deleting an unknown user returns 404",
    ]


@pytest.mark.positive
def test_falls_back_to_any_list_when_there_is_no_heading(extractor):
    text = "Notes from grooming:\n- Orders can be cancelled before shipping\n- Cancelled orders are refunded in full"

    assert extractor.extract(text) == [
        "Orders can be cancelled before shipping",
        "Cancelled orders are refunded in full",
    ]


@pytest.mark.negative
@pytest.mark.parametrize("noise", ["- 404", "- /customers/{id}", "- https://example.com/docs", "- Too short"])
def test_noise_items_are_dropped(extractor, noise):
    text = f"Acceptance Criteria:\n{noise}\n- The customer list is sorted by name"

    assert extractor.extract(text) == ["The customer list is sorted by name"]


@pytest.mark.edge
@pytest.mark.parametrize("text", [None, "", "A description with no list at all."])
def test_no_criteria_without_an_llm_returns_empty(extractor, text):
    assert extractor.extract(text) == []
