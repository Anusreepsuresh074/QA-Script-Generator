# QA Script Generator – Input & Data Processing Module

AI-powered internal QA automation tool that analyses Jira tickets and Swagger/OpenAPI documentation to produce a normalised JSON payload for downstream LLM-based Pytest script generation.

## Architecture

```
POST /process-ticket
        │
        ▼
┌──────────────────┐     ┌─────────────────────┐
│   Jira Client    │────▶│  Ticket Processor    │
│  (REST API v2)   │     │  + Criteria Extractor│
└──────────────────┘     └─────────┬───────────┘
                                   │
┌──────────────────┐               │
│  Swagger Parser  │───────────────┤
│  (2.0 / OAS 3.x)│               │
└──────────────────┘               ▼
                          ┌────────────────┐
                          │   API Mapper   │
                          │ keyword / embed│
                          └───────┬────────┘
                                  │
                                  ▼
                        ProcessedDataSchema
                        (normalised JSON)
```

## Project Structure

```
QA-Script-Generator/
├── app/
│   ├── config/settings.py        # Env-based configuration (Pydantic Settings)
│   ├── models/schemas.py         # Pydantic request / response schemas
│   ├── jira/
│   │   ├── jira_client.py        # Low-level Jira REST client
│   │   ├── ticket_processor.py   # Raw-issue → TicketSchema transformer
│   │   └── criteria_extractor.py # Regex + heuristic AC extraction
│   ├── swagger/
│   │   └── swagger_parser.py     # Swagger 2 / OpenAPI 3 parser
│   ├── mapper/
│   │   └── api_mapper.py         # Keyword & embedding-based mapping
│   ├── services/
│   │   └── processing_service.py # End-to-end pipeline orchestrator
│   ├── utils/
│   │   ├── logger.py             # Centralised logging setup
│   │   └── helpers.py            # Shared helpers (file loading, etc.)
│   └── main.py                   # FastAPI application
├── tests/
│   ├── test_jira.py
│   ├── test_swagger.py
│   └── test_processor.py
├── .env.example
├── requirements.txt
└── README.md
```

## Quick Start

### 1. Clone & install

```bash
cd QA-Script-Generator
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Jira credentials and preferences
```

### 3. Run the server

```bash
uvicorn app.main:app --reload
```

The API is available at **http://localhost:8000**.
Interactive docs at **http://localhost:8000/docs**.

### 4. Run tests

```bash
pytest tests/ -v
```

### 5. Try it offline (no Jira or cloud keys)

`demo/mock_server.py` gives you a fake Jira (tickets `QA-1`, `QA-2`) and a small Customers API with its own OpenAPI spec. Combined with a local [Ollama](https://ollama.com) model, the whole pipeline runs on your laptop.

```bash
ollama pull qwen2.5:7b
uvicorn demo.mock_server:app --port 8100 &
```

Set these in `.env`, then start the app as in step 3:

```bash
JIRA_URL=http://localhost:8100/jira
JIRA_EMAIL=demo@localhost
JIRA_API_TOKEN=local-demo
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_API_KEY=local-not-needed
GROQ_API_KEY=not-configured
SWAGGER_URL=http://localhost:8100/api/openapi.json
API_BASE_URL=http://localhost:8100/api
```

Pick `QA-1` in the dashboard at http://localhost:8000. A 7B model on a CPU takes several minutes, and its scripts often need fixing; a larger hosted model does much better.

## API Reference

### `POST /process-ticket`

Process a Jira ticket with an optional Swagger/OpenAPI spec.

**Request body:**

```json
{
  "ticket_id": "PROJ-123",
  "swagger_url": "https://example.com/swagger.json"
}
```

You can also pass an inline spec dict instead of a URL:

```json
{
  "ticket_id": "PROJ-123",
  "swagger_content": { "openapi": "3.0.0", "..." : "..." }
}
```

**Response:**

```json
{
  "success": true,
  "data": {
    "ticket_id": "PROJ-123",
    "summary": "Create Customer API",
    "description": "As a user I want to create a customer...",
    "acceptance_criteria": [
      "User can create customer",
      "Email is mandatory",
      "Duplicate email throws error"
    ],
    "apis": [
      {
        "endpoint": "/customers",
        "method": "POST",
        "summary": "Create a customer",
        "parameters": [],
        "request_body": {
          "type": "object",
          "required": ["name", "email"],
          "properties": {
            "name": { "type": "string" },
            "email": { "type": "string" }
          }
        },
        "required_fields": ["name", "email"],
        "response_codes": ["200", "400", "401"],
        "authentication_type": "http",
        "examples": null
      }
    ]
  },
  "error": null
}
```

### `GET /health`

Returns `{"status": "ok"}`.

## Configuration

All settings are loaded from environment variables (or `.env`):

| Variable | Default | Description |
|---|---|---|
| `JIRA_URL` | *required* | Jira base URL |
| `JIRA_EMAIL` | *required* | Jira account email |
| `JIRA_API_TOKEN` | *required* | Jira API token |
| `JIRA_REQUEST_TIMEOUT` | `30` | HTTP timeout for Jira (seconds) |
| `SWAGGER_REQUEST_TIMEOUT` | `30` | HTTP timeout for spec fetches |
| `MAPPING_TYPE` | `keyword` | `keyword`, `embedding`, or `ollama` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model name (run `ollama pull` first) |
| `OLLAMA_API_KEY` | *required* | Ollama API key (sent on every LLM request) |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model name |
| `LOG_LEVEL` | `INFO` | Python log level |
| `APP_HOST` | `0.0.0.0` | Uvicorn bind host |
| `APP_PORT` | `8000` | Uvicorn bind port |

## Mapping Strategies

### Keyword (default)

Tokenises acceptance criteria and endpoint metadata, scores by token overlap. Fast, no model download required.

### Ollama (recommended)

Uses a local Ollama instance for semantic API matching, criteria extraction, scenario generation, and script generation. Set `MAPPING_TYPE=ollama`, provide `OLLAMA_API_KEY`, and ensure Ollama is running with the configured model pulled:

```bash
ollama serve          # if not already running
ollama pull llama3.2  # or your chosen OLLAMA_MODEL
```

### Embedding

Uses `sentence-transformers` to compute cosine similarity between criteria text and endpoint descriptions. More accurate for loosely worded criteria than keyword matching. Set `MAPPING_TYPE=embedding`; the model is downloaded on first use.

## Error Handling

| HTTP Code | Meaning |
|---|---|
| `401` | Invalid Jira credentials |
| `404` | Jira ticket not found |
| `422` | Swagger spec parse failure |
| `502` | Jira unreachable / timeout |
| `500` | Unexpected server error |

## About

A learning project on AI agents for QA automation, built with two teammates ([@iszac01](https://github.com/iszac01), [@jenieraju](https://github.com/jenieraju)).

My parts: multi-protocol parsers (GraphQL, SOAP, gRPC, WebSocket), the Robot Framework / Jest / Postman generators, the Slack → Jira ticket agent (`jira-ticket-agent/`), and the offline demo (`demo/`).
