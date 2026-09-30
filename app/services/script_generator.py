"""
Phase 3 – LLM-powered script generator.

Supports four test frameworks:
  pytest    – Python pytest + requests/zeep/grpcio/websockets (default)
  robot     – Robot Framework keyword-driven .robot files
  jest      – JavaScript Jest + axios
  postman   – Postman Collection v2.1 JSON (run with Newman)
"""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any

from app.ai.llm_client import LLMClient, LLMTransientError
from app.config.settings import Settings, get_settings
from app.models.schemas import (
    APIType,
    GeneratedScenariosSchema,
    GeneratedScriptsSchema,
    ProcessedDataSchema,
    TestFramework,
)
from app.utils.jest_templates import (
    JEST_STANDARDS_BY_TYPE,
    build_jest_config,
    build_jest_setup,
    build_package_json,
    jest_test_filename,
    validate_jest_source,
)
from app.utils.output_writer import write_scripts
from app.utils.postman_templates import (
    POSTMAN_STANDARDS_BY_TYPE,
    postman_collection_filename,
    postman_collection_skeleton,
    validate_postman_collection,
)
from app.utils.pytest_templates import QA_STANDARDS_BY_TYPE, build_conftest, build_pytest_ini
from app.utils.robot_templates import (
    ROBOT_STANDARDS_BY_TYPE,
    build_robot_resources,
    validate_robot_source,
)

logger = logging.getLogger(__name__)


class ScriptGenerationError(Exception):
    pass


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_python(raw: str) -> str:
    text = raw.strip()
    m = re.match(r"^```(?:python)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text


def _extract_code(raw: str, language: str = "") -> str:
    """Strip markdown fences regardless of language tag."""
    text = raw.strip()
    pattern = rf"^```(?:{re.escape(language)})?\s*\n?(.*?)\n?```\s*$"
    m = re.match(pattern, text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text


def _validate_python(source: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise ScriptGenerationError(f"Generated Python code has syntax errors: {exc}") from exc


def _pytest_test_filename(ticket_id: str) -> str:
    return f"test_{ticket_id.lower().replace('-', '_')}.py"


def _robot_test_filename(ticket_id: str) -> str:
    return f"test_{ticket_id.lower().replace('-', '_')}.robot"


# ---------------------------------------------------------------------------
# Per-framework prompt builders
# ---------------------------------------------------------------------------

def _common_context(processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str) -> tuple[str, str, str]:
    """Return (criteria_json, api_json, scenario_json) strings."""
    api_payload = []
    for a in processed.apis:
        entry: dict[str, Any] = {
            "api_type": a.api_type.value,
            "endpoint": a.endpoint,
            "method": a.method,
            "summary": a.summary,
            "required_fields": a.required_fields,
        }
        if a.operation_name:
            entry["operation_name"] = a.operation_name
        if a.service_name:
            entry["service_name"] = a.service_name
        if a.rpc_type:
            entry["rpc_type"] = a.rpc_type
        if a.channel:
            entry["channel"] = a.channel
        if a.message_type:
            entry["message_type"] = a.message_type
        if processed.api_type == APIType.REST:
            entry["response_codes"] = a.response_codes
        api_payload.append(entry)

    scenario_payload = [
        {
            "name": s.name,
            "type": s.type.value,
            "description": s.description,
            "steps": s.steps,
            "expected_result": s.expected_result,
            "api_endpoint": s.api_endpoint,
            "method": s.method,
        }
        for s in scenarios.scenarios
    ]
    return (
        json.dumps(processed.acceptance_criteria, indent=2),
        json.dumps(api_payload, indent=2),
        json.dumps(scenario_payload, indent=2),
    )


def _build_pytest_prompt(processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str) -> str:
    criteria, apis, scens = _common_context(processed, scenarios, base_url)
    api_type = processed.api_type
    url_label = {
        APIType.GRAPHQL: "GraphQL endpoint (use with graphql_url fixture)",
        APIType.SOAP: "WSDL URL (use with wsdl_url fixture)",
        APIType.GRPC: "gRPC server (use with grpc_channel fixture)",
        APIType.WEBSOCKET: "WebSocket URL (use with ws_url fixture)",
    }.get(api_type, "API base URL (use with base_url fixture)")
    return f"""Ticket: {processed.ticket_id}
Summary: {processed.summary}
API Type: {api_type.value.upper()}
{url_label}: {base_url}

Acceptance criteria:
{criteria}

Mapped API operations:
{apis}

Test scenarios (implement one pytest function per scenario):
{scens}

Generate a complete pytest module named {_pytest_test_filename(processed.ticket_id)} implementing all scenarios."""


def _build_robot_prompt(processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str) -> str:
    criteria, apis, scens = _common_context(processed, scenarios, base_url)
    return f"""Ticket: {processed.ticket_id}
Summary: {processed.summary}
API Type: {processed.api_type.value.upper()}
Base URL variable: ${{BASE_URL}} (default: {base_url})

Acceptance criteria:
{criteria}

Mapped API operations:
{apis}

Test scenarios (implement one Robot Framework Test Case per scenario):
{scens}

Generate a complete Robot Framework .robot file named {_robot_test_filename(processed.ticket_id)} implementing all scenarios.
The resources.robot file is already provided — import it with: Resource    resources.robot"""


def _build_jest_prompt(processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str) -> str:
    criteria, apis, scens = _common_context(processed, scenarios, base_url)
    return f"""Ticket: {processed.ticket_id}
Summary: {processed.summary}
API Type: {processed.api_type.value.upper()}
Base URL (from setup.js): {base_url}

Acceptance criteria:
{criteria}

Mapped API operations:
{apis}

Test scenarios (implement one test() per scenario):
{scens}

Generate a complete Jest test file named {jest_test_filename(processed.ticket_id)} implementing all scenarios.
The setup.js file is already provided — import it with: const {{ BASE_URL, createClient }} = require('./setup');"""


def _build_postman_prompt(processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str) -> str:
    criteria, apis, scens = _common_context(processed, scenarios, base_url)
    skeleton = postman_collection_skeleton(processed.ticket_id, processed.summary, base_url)
    return f"""Ticket: {processed.ticket_id}
Summary: {processed.summary}
API Type: {processed.api_type.value.upper()}
Base URL: {base_url}

Acceptance criteria:
{criteria}

Mapped API operations:
{apis}

Test scenarios (implement one Postman request item per scenario):
{scens}

Generate a complete Postman Collection v2.1 JSON implementing ALL scenarios.
Follow this structure exactly:
{skeleton}

Each item must correspond to exactly one scenario from the list above.
Output ONLY the JSON object — no markdown, no explanation."""


# ---------------------------------------------------------------------------
# ScriptGenerator
# ---------------------------------------------------------------------------

class ScriptGenerator:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._llm = LLMClient(self._settings)

    def generate(
        self,
        processed: ProcessedDataSchema,
        scenarios: GeneratedScenariosSchema,
        base_url: str | None = None,
        framework: TestFramework = TestFramework.PYTEST,
    ) -> GeneratedScriptsSchema:
        resolved_base = (base_url or self._settings.api_base_url).rstrip("/")

        if framework == TestFramework.ROBOT:
            return self._generate_robot(processed, scenarios, resolved_base)
        if framework == TestFramework.JEST:
            return self._generate_jest(processed, scenarios, resolved_base)
        if framework == TestFramework.POSTMAN:
            return self._generate_postman(processed, scenarios, resolved_base)
        return self._generate_pytest(processed, scenarios, resolved_base)

    # -- pytest ----------------------------------------------------------------

    def _generate_pytest(
        self, processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str
    ) -> GeneratedScriptsSchema:
        api_type = processed.api_type
        qa_standards = QA_STANDARDS_BY_TYPE.get(api_type, QA_STANDARDS_BY_TYPE[APIType.REST])
        prompt = _build_pytest_prompt(processed, scenarios, base_url)
        test_source = _extract_python(self._call_llm(prompt, qa_standards))
        _validate_python(test_source)

        conftest_source = build_conftest(
            processed.ticket_id, base_url, self._settings.api_key_default, api_type=api_type
        )
        _validate_python(conftest_source)

        files = [
            ("conftest.py", conftest_source),
            ("pytest.ini", build_pytest_ini()),
            (_pytest_test_filename(processed.ticket_id), test_source),
        ]
        return self._write(processed, scenarios, files, TestFramework.PYTEST)

    # -- Robot Framework -------------------------------------------------------

    def _generate_robot(
        self, processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str
    ) -> GeneratedScriptsSchema:
        api_type = processed.api_type
        standards = ROBOT_STANDARDS_BY_TYPE.get(api_type, ROBOT_STANDARDS_BY_TYPE[APIType.REST])
        prompt = _build_robot_prompt(processed, scenarios, base_url)
        robot_source = _extract_code(self._call_llm(prompt, standards), "robot")
        validate_robot_source(robot_source)

        resources_source = build_robot_resources(
            processed.ticket_id, base_url, self._settings.api_key_default, api_type=api_type
        )
        files = [
            ("resources.robot", resources_source),
            (_robot_test_filename(processed.ticket_id), robot_source),
        ]
        return self._write(processed, scenarios, files, TestFramework.ROBOT)

    # -- Jest ------------------------------------------------------------------

    def _generate_jest(
        self, processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str
    ) -> GeneratedScriptsSchema:
        api_type = processed.api_type
        standards = JEST_STANDARDS_BY_TYPE.get(api_type, JEST_STANDARDS_BY_TYPE[APIType.REST])
        prompt = _build_jest_prompt(processed, scenarios, base_url)
        jest_source = _extract_code(self._call_llm(prompt, standards), "javascript")
        validate_jest_source(jest_source)

        files = [
            ("setup.js", build_jest_setup(processed.ticket_id, base_url, api_type)),
            ("jest.config.js", build_jest_config()),
            ("package.json", build_package_json(processed.ticket_id)),
            (jest_test_filename(processed.ticket_id), jest_source),
        ]
        return self._write(processed, scenarios, files, TestFramework.JEST)

    # -- Postman ---------------------------------------------------------------

    def _generate_postman(
        self, processed: ProcessedDataSchema, scenarios: GeneratedScenariosSchema, base_url: str
    ) -> GeneratedScriptsSchema:
        api_type = processed.api_type
        standards = POSTMAN_STANDARDS_BY_TYPE.get(api_type, POSTMAN_STANDARDS_BY_TYPE[APIType.REST])
        prompt = _build_postman_prompt(processed, scenarios, base_url)
        raw = self._call_llm(prompt, standards)
        collection_source = _extract_code(raw, "json")
        validate_postman_collection(collection_source)

        files = [(postman_collection_filename(processed.ticket_id), collection_source)]
        return self._write(processed, scenarios, files, TestFramework.POSTMAN)

    # -- shared ----------------------------------------------------------------

    def _write(
        self,
        processed: ProcessedDataSchema,
        scenarios: GeneratedScenariosSchema,
        files: list[tuple[str, str]],
        framework: TestFramework,
    ) -> GeneratedScriptsSchema:
        result = write_scripts(
            processed.ticket_id,
            processed.summary,
            files,
            settings=self._settings,
            framework=framework,
        )
        logger.info(
            "Generated %d file(s) for %s [%s] in %s",
            len(result.files),
            processed.ticket_id,
            framework.value,
            result.output_dir,
        )
        return result

    def _call_llm(self, user_prompt: str, system_prompt: str) -> str:
        try:
            return self._llm.chat_completion(
                system=system_prompt,
                user=user_prompt,
                temperature=0.2,
            )
        except LLMTransientError as exc:
            raise ScriptGenerationError(
                "LLM unavailable — Ollama and Groq both failed, try again in a few minutes"
            ) from exc
        except ValueError as exc:
            raise ScriptGenerationError(str(exc)) from exc
