"""The offline demo Customers API behaves as its tickets describe, so the
tests the tool generates against it have a correct target."""

import pytest
from fastapi.testclient import TestClient

from demo import mock_server


@pytest.fixture
def client():
    mock_server._customers.clear()
    return TestClient(mock_server.app)


@pytest.mark.positive
def test_create_then_fetch_a_customer(client):
    created = client.post("/api/customers", json={"name": "Asha", "email": "asha@example.com"})
    assert created.status_code == 201

    customer_id = created.json()["id"]
    fetched = client.get(f"/api/customers/{customer_id}")
    assert fetched.status_code == 200
    assert fetched.json() == {"id": customer_id, "name": "Asha", "email": "asha@example.com"}


@pytest.mark.positive
def test_update_and_delete_a_customer(client):
    customer_id = client.post("/api/customers", json={"name": "Ben", "email": "ben@example.com"}).json()["id"]

    updated = client.put(f"/api/customers/{customer_id}", json={"name": "Benjamin"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Benjamin"

    assert client.delete(f"/api/customers/{customer_id}").status_code == 204
    assert client.get(f"/api/customers/{customer_id}").status_code == 404


@pytest.mark.negative
def test_duplicate_email_is_rejected_case_insensitively(client):
    client.post("/api/customers", json={"name": "Asha", "email": "asha@example.com"})

    duplicate = client.post("/api/customers", json={"name": "Other", "email": "ASHA@example.com"})
    assert duplicate.status_code == 409


@pytest.mark.negative
@pytest.mark.parametrize(
    "body",
    [
        {"name": "Asha"},
        {"email": "asha@example.com"},
        {"name": "", "email": "asha@example.com"},
        {"name": "Asha", "email": "not-an-email"},
    ],
)
def test_invalid_create_bodies_return_422(client, body):
    assert client.post("/api/customers", json=body).status_code == 422


@pytest.mark.negative
def test_unknown_ticket_returns_404_from_fake_jira(client):
    assert client.get("/jira/rest/api/3/issue/QA-999").status_code == 404
