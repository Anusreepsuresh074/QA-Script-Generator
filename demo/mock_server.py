"""Local stand-ins so QA-Script-Generator runs with no real Jira account.

Serves two things on one port (default 8100):
  /jira/...  a fake Jira Cloud (only the endpoints the app calls)
  /api/...   a small in-memory Customers REST API for generated tests to hit
             (spec at /api/openapi.json)

Run (from the repo root):  uvicorn demo.mock_server:app --port 8100
"""

import itertools
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

# ---------------------------------------------------------------------------
# Fake Jira
# ---------------------------------------------------------------------------

TICKETS: Dict[str, dict] = {
    "QA-1": {
        "summary": "Create Customer API",
        "type": "Story",
        "status": "To Do",
        "labels": ["api", "customers"],
        "description": (
            "As an API client I want to create customers so that they can place orders.\n\n"
            "Acceptance Criteria:\n"
            "- User can create a customer with name and email via POST /customers\n"
            "- Email is mandatory and must be a valid email address\n"
            "- Name is mandatory and must not be empty\n"
            "- Creating a customer with a duplicate email returns 409 Conflict\n"
            "- A successful create returns 201 with the new customer id\n"
        ),
    },
    "QA-2": {
        "summary": "Get, update and delete Customer API",
        "type": "Story",
        "status": "In Progress",
        "labels": ["api", "customers"],
        "description": (
            "As an API client I want to read, update and delete customers.\n\n"
            "Acceptance Criteria:\n"
            "- User can fetch a customer by id via GET /customers/{id}\n"
            "- Fetching a customer id that does not exist returns 404\n"
            "- User can list all customers via GET /customers\n"
            "- User can update a customer's name via PUT /customers/{id}\n"
            "- User can delete a customer via DELETE /customers/{id} and it returns 204\n"
        ),
    },
}

jira = FastAPI(title="Fake Jira")
_comment_ids = itertools.count(1)


def _issue(key: str) -> dict:
    t = TICKETS[key]
    return {
        "key": key,
        "id": key.split("-")[1],
        "fields": {
            "summary": t["summary"],
            "description": t["description"],
            "labels": t["labels"],
            "status": {"name": t["status"]},
            "issuetype": {"name": t["type"]},
            "priority": {"name": "Medium"},
            "issuelinks": [],
            "attachment": [],
            "comment": {"comments": []},
        },
    }


@jira.get("/rest/api/{ver}/myself")
def myself(ver: str):
    return {"displayName": "Local Demo User", "emailAddress": "demo@localhost"}


@jira.get("/rest/api/{ver}/issue/{key}")
def get_issue(ver: str, key: str):
    if key.upper() not in TICKETS:
        return JSONResponse({"errorMessages": ["Issue does not exist"]}, status_code=404)
    return _issue(key.upper())


@jira.get("/rest/api/{ver}/search")
@jira.post("/rest/api/{ver}/search/jql")
def search(ver: str):
    return {"issues": [_issue(k) for k in TICKETS], "total": len(TICKETS)}


@jira.post("/rest/api/{ver}/issue/{key}/comment")
def add_comment(ver: str, key: str):
    return {"id": str(next(_comment_ids))}


@jira.post("/rest/api/{ver}/issue/{key}/attachments")
async def add_attachment(ver: str, key: str, request: Request):
    return [{"id": str(next(_comment_ids)), "filename": "report"}]


# ---------------------------------------------------------------------------
# Demo Customers API
# ---------------------------------------------------------------------------

api = FastAPI(
    title="Customers API",
    version="1.0.0",
    servers=[{"url": "http://localhost:8100/api"}],
)


class CustomerIn(BaseModel):
    name: str = Field(..., min_length=1)
    email: str = Field(..., pattern=EMAIL_RE)


class CustomerUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    email: Optional[str] = Field(None, pattern=EMAIL_RE)


class Customer(CustomerIn):
    id: int


_customers: Dict[int, Customer] = {}
_ids = itertools.count(1)


@api.post("/customers", response_model=Customer, status_code=201,
          summary="Create a customer", responses={409: {"description": "Duplicate email"}})
def create_customer(body: CustomerIn):
    if any(c.email.lower() == body.email.lower() for c in _customers.values()):
        raise HTTPException(409, "Customer with this email already exists")
    c = Customer(id=next(_ids), **body.model_dump())
    _customers[c.id] = c
    return c


@api.get("/customers", response_model=List[Customer], summary="List customers")
def list_customers():
    return list(_customers.values())


@api.get("/customers/{customer_id}", response_model=Customer, summary="Get a customer by id",
         responses={404: {"description": "Not found"}})
def get_customer(customer_id: int):
    if customer_id not in _customers:
        raise HTTPException(404, "Customer not found")
    return _customers[customer_id]


@api.put("/customers/{customer_id}", response_model=Customer, summary="Update a customer",
         responses={404: {"description": "Not found"}, 409: {"description": "Duplicate email"}})
def update_customer(customer_id: int, body: CustomerUpdate):
    if customer_id not in _customers:
        raise HTTPException(404, "Customer not found")
    data = _customers[customer_id].model_dump()
    data.update(body.model_dump(exclude_none=True))
    _customers[customer_id] = Customer(**data)
    return _customers[customer_id]


@api.delete("/customers/{customer_id}", status_code=204, summary="Delete a customer",
            responses={404: {"description": "Not found"}})
def delete_customer(customer_id: int):
    if _customers.pop(customer_id, None) is None:
        raise HTTPException(404, "Customer not found")


# ---------------------------------------------------------------------------

app = FastAPI(title="QA-Script-Generator local mocks")
app.mount("/jira", jira)
app.mount("/api", api)


@app.get("/")
def index():
    return {"jira": "/jira", "customers_api": "/api", "spec": "/api/openapi.json",
            "tickets": list(TICKETS)}
