"""End to end, offline: a demo Jira ticket and the demo OpenAPI spec go in,
and the normalised payload for the LLM comes out."""

import pytest

from app.services.processing_service import ProcessingService
from demo.mock_server import _issue
from demo.mock_server import api as customers_api


class FakeJira:
    def fetch_ticket(self, ticket_id: str) -> dict:
        return _issue(ticket_id)


@pytest.mark.positive
def test_ticket_and_spec_become_a_processed_payload():
    result = ProcessingService(jira_client=FakeJira()).process_ticket(
        ticket_id="QA-1",
        swagger_source=customers_api.openapi(),
    )

    assert result.ticket_id == "QA-1"
    assert result.summary == "Create Customer API"
    assert len(result.acceptance_criteria) == 5
    assert ("POST", "/customers") in {(a.method, a.endpoint) for a in result.apis}
