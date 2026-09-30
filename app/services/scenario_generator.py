"""
LLM-powered test scenario generator.
Takes ProcessedDataSchema (ticket + mapped APIs) and produces
positive, negative, and edge test scenarios via Groq.
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any

from app.ai.llm_client import LLMClient, LLMTransientError
from app.config.settings import get_settings
from app.models.schemas import (
    APIType,
    GeneratedScenariosSchema,
    ProcessedDataSchema,
    TestScenario,
    TestScenarioType,
)

logger = logging.getLogger(__name__)

_BASE_RULES = """Rules:
- Generate only scenarios grounded in the acceptance criteria (and API contract when available).
- Prefer the smallest useful set: one scenario per distinct behaviour. Do not manufacture scenarios just to fill scenario types.
- Do not repeat behaviour with cosmetic changes to names, data values, wording, or steps.
- Skip generic performance/concurrency/load scenarios unless the ticket explicitly asks for them.
- If multiple acceptance criteria describe the same behaviour, cover them with one scenario.
- When acceptance criteria are present, ALWAYS return at least one valid scenario — never return an empty scenarios array.
- Each scenario must have a clear name, type, description, step-by-step actions, and expected result.
- Prefer linking scenarios to a mapped API endpoint/method when one fits; otherwise set api_endpoint/method to null.
- steps must be an array of strings.
- type must be exactly one of: positive, negative, edge
- Output ONLY valid JSON with this exact shape (no markdown, no explanation):
{{"scenarios": [{{"name": "...", "type": "positive|negative|edge", "description": "...", "steps": ["..."], "expected_result": "...", "api_endpoint": "... or null", "method": "... or null"}}]}}"""

_CRITERIA_BATCH_SIZE = 5

# Lower priority = kept first when trimming to the cap.
_TYPE_PRIORITY = {
    TestScenarioType.POSITIVE: 0,
    TestScenarioType.NEGATIVE: 1,
    TestScenarioType.EDGE: 2,
}

_TYPE_ALIASES = {
    "positive": "positive",
    "happy": "positive",
    "happy_path": "positive",
    "functional": "positive",
    "success": "positive",
    "negative": "negative",
    "error": "negative",
    "failure": "negative",
    "invalid": "negative",
    "edge": "edge",
    "boundary": "edge",
    "edge_case": "edge",
}

_SYSTEM_PROMPTS: dict[APIType, str] = {
    APIType.REST: f"""You are a senior QA engineer. Given a Jira ticket and mapped REST API endpoints, generate a concise set of high-value test scenarios.

Cover each distinct required behaviour once. Add a negative or edge scenario only when a specific validation rule, error response, required field, boundary, or failure behaviour is present in the ticket or API contract.

{_BASE_RULES}""",

    APIType.GRAPHQL: f"""You are a senior QA engineer specialising in GraphQL API testing. Given a Jira ticket and mapped GraphQL operations, generate a concise set of high-value test scenarios.

Cover each distinct required behaviour once. Add negative or edge coverage only for constraints supported by the ticket or schema.

{_BASE_RULES}""",

    APIType.SOAP: f"""You are a senior QA engineer specialising in SOAP/WSDL API testing. Given a Jira ticket and mapped SOAP operations, generate a concise set of high-value test scenarios.

Cover each distinct required behaviour once. Add negative or edge coverage only for constraints supported by the ticket or WSDL.

{_BASE_RULES}""",

    APIType.GRPC: f"""You are a senior QA engineer specialising in gRPC/Protobuf API testing. Given a Jira ticket and mapped gRPC service methods, generate a concise set of high-value test scenarios.

Cover each distinct required behaviour once. Add negative or edge coverage only for constraints supported by the ticket or Protobuf contract.

For "method" field use the RPC type: unary | server_streaming | client_streaming | bidi_streaming.
{_BASE_RULES}""",

    APIType.WEBSOCKET: f"""You are a senior QA engineer specialising in WebSocket/AsyncAPI testing. Given a Jira ticket and mapped WebSocket channels/events, generate a concise set of high-value test scenarios.

Cover each distinct required behaviour once. Add negative or edge coverage only for constraints supported by the ticket or AsyncAPI contract.

For "method" field use: send | receive | subscribe | connect | disconnect.
{_BASE_RULES}""",
}


def _build_api_block(data: ProcessedDataSchema) -> str:
    api_type = data.api_type
    if not data.apis:
        return "No API spec provided — generate scenarios based on ticket context only."

    lines = []
    if api_type == APIType.GRAPHQL:
        header = "Mapped GraphQL Operations:"
        for api in data.apis:
            score = f" (match: {api.match_score:.0f}%)" if api.match_score is not None else ""
            args = ", ".join(
                f"{p['name']}: {p.get('type', '?')}{'!' if p.get('required') else ''}"
                for p in api.parameters
            )
            lines.append(
                f"  {api.method} {api.endpoint}({args}){score}"
                + (f" — {api.summary}" if api.summary else "")
            )
    elif api_type == APIType.SOAP:
        header = "Mapped SOAP Operations:"
        for api in data.apis:
            score = f" (match: {api.match_score:.0f}%)" if api.match_score is not None else ""
            lines.append(
                f"  {api.operation_name}{score} @ {api.endpoint}"
                + (f" | service: {api.service_name}" if api.service_name else "")
                + (f" | required elements: {', '.join(api.required_fields)}" if api.required_fields else "")
            )
    elif api_type == APIType.GRPC:
        header = "Mapped gRPC Methods:"
        for api in data.apis:
            score = f" (match: {api.match_score:.0f}%)" if api.match_score is not None else ""
            lines.append(
                f"  {api.endpoint} [{api.rpc_type or api.method}]{score}"
                + (f" — {api.summary}" if api.summary else "")
                + (f" | required fields: {', '.join(api.required_fields)}" if api.required_fields else "")
            )
    elif api_type == APIType.WEBSOCKET:
        header = "Mapped WebSocket Channels:"
        for api in data.apis:
            score = f" (match: {api.match_score:.0f}%)" if api.match_score is not None else ""
            lines.append(
                f"  {api.method} {api.channel or api.endpoint}{score}"
                + (f" message: {api.message_type}" if api.message_type else "")
                + (f" — {api.summary}" if api.summary else "")
            )
    else:
        header = "Mapped REST Endpoints:"
        for api in data.apis:
            score = f" (match: {api.match_score:.0f}%)" if api.match_score is not None else ""
            lines.append(
                f"  {api.method.upper()} {api.endpoint}{score}"
                + (f" — {api.summary}" if api.summary else "")
                + (f" | required: {', '.join(api.required_fields)}" if api.required_fields else "")
                + (f" | responses: {', '.join(api.response_codes)}" if api.response_codes else "")
            )

    return header + "\n" + "\n".join(lines)


def _build_prompt(data: ProcessedDataSchema) -> str:
    criteria_block = "\n".join(f"- {c}" for c in data.acceptance_criteria) or "No explicit criteria found."
    api_block = _build_api_block(data)

    return f"""Ticket: {data.ticket_id}
Summary: {data.summary}
API Type: {data.api_type.value.upper()}

Acceptance Criteria:
{criteria_block}

{api_block}

Generate at least one scenario for the criteria above. Prefer mapped endpoints when they fit; otherwise leave api_endpoint null."""


def _parse_scenarios(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []

    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL | re.IGNORECASE)
    if m:
        text = m.group(1).strip()

    def _extract_items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("scenarios", "test_scenarios", "tests", "items", "data"):
                items = data.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    try:
        return _extract_items(json.loads(text))
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return _extract_items(json.loads(text[start: end + 1]))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return _extract_items(json.loads(text[start: end + 1]))
        except json.JSONDecodeError:
            pass

    return []


def _normalize_scenario_type(raw: Any) -> TestScenarioType:
    value = str(raw or "positive").strip().lower().replace(" ", "_").replace("-", "_")
    mapped = _TYPE_ALIASES.get(value, value)
    try:
        return TestScenarioType(mapped)
    except ValueError:
        return TestScenarioType.POSITIVE


def _normalized_text(value: Any) -> str:
    """Return stable text used for duplicate and relevance comparisons."""
    text = re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())
    return " ".join(text.split())


def _dedupe_criteria(criteria: list[str]) -> list[str]:
    """Remove repeated or nearly identical acceptance criteria before batching."""
    unique: list[str] = []
    normalized: list[str] = []
    for criterion in criteria:
        value = str(criterion or "").strip()
        candidate = _normalized_text(value)
        if not candidate:
            continue
        if any(
            candidate == existing
            or SequenceMatcher(None, candidate, existing).ratio() >= 0.9
            for existing in normalized
        ):
            logger.info("Skipping duplicate acceptance criterion: %s", value)
            continue
        unique.append(value)
        normalized.append(candidate)
    return unique


def _scenario_text(scenario: TestScenario) -> str:
    return _normalized_text(
        " ".join(
            [
                scenario.name,
                scenario.description,
                scenario.expected_result,
                *scenario.steps,
            ]
        )
    )


def _same_target(left: TestScenario, right: TestScenario) -> bool:
    return (
        _normalized_text(left.api_endpoint) == _normalized_text(right.api_endpoint)
        and _normalized_text(left.method) == _normalized_text(right.method)
        and left.type == right.type
    )


def _is_duplicate_scenario(candidate: TestScenario, existing: TestScenario) -> bool:
    """Detect duplicates despite minor LLM wording differences."""
    if not _same_target(candidate, existing):
        return False

    left = _scenario_text(candidate)
    right = _scenario_text(existing)
    if not left or not right:
        return False
    if left == right or SequenceMatcher(None, left, right).ratio() >= 0.82:
        return True

    left_name = set(_normalized_text(candidate.name).split())
    right_name = set(_normalized_text(existing.name).split())
    name_union = left_name | right_name
    name_overlap = (
        len(left_name & right_name) / len(name_union) if name_union else 0
    )
    if name_overlap >= 0.75:
        return True

    left_tokens = set(left.split())
    right_tokens = set(right.split())
    union = left_tokens | right_tokens
    overlap = len(left_tokens & right_tokens) / len(union) if union else 0
    return overlap >= 0.78


_GENERIC_SCENARIO_TERMS = {
    "concurrent": ("concurrent", "parallel", "simultaneous"),
    "performance": ("performance", "latency", "response time", "throughput"),
    "load": ("load test", "stress test", "high traffic"),
    "large_payload": ("large payload", "very large", "maximum payload"),
    "special_characters": ("special character", "unicode", "sql injection"),
    "rate_limit": ("rate limit", "throttl", "too many requests"),
}


def _supported_context(data: ProcessedDataSchema) -> str:
    api_parts: list[str] = []
    for api in data.apis:
        api_parts.extend(
            [
                str(api.endpoint or ""),
                str(api.method or ""),
                str(api.summary or ""),
                " ".join(api.required_fields),
                " ".join(api.response_codes),
            ]
        )
    return _normalized_text(
        " ".join([data.summary, *data.acceptance_criteria, *api_parts])
    )


def _is_unsupported_generic(scenario: TestScenario, context: str) -> bool:
    scenario_text = _scenario_text(scenario)
    for aliases in _GENERIC_SCENARIO_TERMS.values():
        if any(term in scenario_text for term in aliases) and not any(
            term in context for term in aliases
        ):
            return True
    return False


def _best_fallback_api(data: ProcessedDataSchema):
    """Pick the highest-scoring mapped API for scenarios missing a valid endpoint."""
    if not data.apis:
        return None
    return max(
        data.apis,
        key=lambda api: (
            api.match_score if api.match_score is not None else -1.0,
            1 if api.matched else 0,
        ),
    )


def _attach_fallback_endpoint(
    scenario: TestScenario,
    data: ProcessedDataSchema,
) -> TestScenario:
    """Keep scenarios even when endpoint is missing/unmapped by linking a mapped API."""
    allowed = {
        _normalized_text(api.channel or api.operation_name or api.endpoint): api
        for api in data.apis
        if api.channel or api.operation_name or api.endpoint
    }
    endpoint = _normalized_text(scenario.api_endpoint)
    if endpoint and endpoint in allowed:
        return scenario

    fallback = _best_fallback_api(data)
    if fallback is None:
        # No mapped APIs — keep ticket-context scenarios as-is
        return scenario

    linked = scenario.model_copy(
        update={
            "api_endpoint": fallback.channel
            or fallback.operation_name
            or fallback.endpoint
            or scenario.api_endpoint,
            "method": fallback.method or scenario.method,
        }
    )
    logger.info(
        "Linked scenario %r to mapped API %s %s (was %r)",
        scenario.name,
        linked.method,
        linked.api_endpoint,
        scenario.api_endpoint,
    )
    return linked


def _filter_scenarios(
    scenarios: list[TestScenario],
    data: ProcessedDataSchema,
) -> list[TestScenario]:
    """Keep complete, non-duplicate scenarios; auto-link endpoints when needed."""
    context = _supported_context(data)
    filtered: list[TestScenario] = []

    for scenario in scenarios:
        if not (
            scenario.name.strip()
            and scenario.description.strip()
            and scenario.expected_result.strip()
            and scenario.steps
            and all(isinstance(step, str) and step.strip() for step in scenario.steps)
        ):
            logger.info("Skipping incomplete scenario: %s", scenario.name)
            continue

        if _is_unsupported_generic(scenario, context):
            logger.info("Skipping unsupported generic scenario: %s", scenario.name)
            continue

        scenario = _attach_fallback_endpoint(scenario, data)

        if any(_is_duplicate_scenario(scenario, item) for item in filtered):
            logger.info("Skipping duplicate scenario: %s", scenario.name)
            continue

        filtered.append(scenario)

    return filtered


class ScenarioGenerationError(Exception):
    pass


class ScenarioGenerator:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._llm = LLMClient(self._settings)

    def generate(self, data: ProcessedDataSchema) -> GeneratedScenariosSchema:
        criteria = _dedupe_criteria(data.acceptance_criteria or [])
        generation_data = data.model_copy(update={"acceptance_criteria": criteria})
        if len(criteria) <= _CRITERIA_BATCH_SIZE:
            scenarios = self._generate_for_criteria(generation_data)
        else:
            logger.info(
                "Batching scenario generation for %s (%d criteria, batch size %d)",
                data.ticket_id,
                len(criteria),
                _CRITERIA_BATCH_SIZE,
            )
            scenarios = []
            for offset in range(0, len(criteria), _CRITERIA_BATCH_SIZE):
                batch = criteria[offset: offset + _CRITERIA_BATCH_SIZE]
                batch_data = generation_data.model_copy(update={"acceptance_criteria": batch})
                scenarios.extend(self._generate_for_criteria(batch_data))

        generated_count = len(scenarios)
        scenarios = _filter_scenarios(scenarios, generation_data)
        logger.info(
            "Scenario quality filter retained %d/%d scenarios for %s",
            len(scenarios),
            generated_count,
            data.ticket_id,
        )

        if not scenarios:
            raise ScenarioGenerationError(
                "LLM returned no valid, unique scenarios supported by the ticket and API contract"
            )

        logger.info(
            "Generated %d scenarios for %s (positive=%d negative=%d edge=%d)",
            len(scenarios),
            data.ticket_id,
            sum(1 for s in scenarios if s.type == TestScenarioType.POSITIVE),
            sum(1 for s in scenarios if s.type == TestScenarioType.NEGATIVE),
            sum(1 for s in scenarios if s.type == TestScenarioType.EDGE),
        )

        return GeneratedScenariosSchema(
            ticket_id=data.ticket_id,
            summary=data.summary,
            scenarios=scenarios,
        )

    def _generate_for_criteria(self, data: ProcessedDataSchema) -> list[TestScenario]:
        prompt = _build_prompt(data)
        system_prompt = _SYSTEM_PROMPTS.get(data.api_type, _SYSTEM_PROMPTS[APIType.REST])
        raw = self._call_llm(prompt, system_prompt)
        raw_scenarios = _parse_scenarios(raw)

        if not raw_scenarios:
            logger.warning(
                "Could not parse scenarios for %s (batch size %d). Raw preview: %s",
                data.ticket_id,
                len(data.acceptance_criteria),
                raw[:300],
            )
            return []

        scenarios: list[TestScenario] = []
        for item in raw_scenarios:
            steps = item.get("steps", [])
            if isinstance(steps, str):
                steps = [s.strip() for s in steps.split("\n") if s.strip()]
            elif isinstance(steps, list):
                steps = [str(s).strip() for s in steps if str(s).strip()]
            else:
                steps = []

            scenarios.append(TestScenario(
                name=str(item.get("name", "")).strip(),
                type=_normalize_scenario_type(item.get("type")),
                description=str(item.get("description", "")).strip(),
                steps=steps,
                expected_result=str(item.get("expected_result", "")).strip(),
                api_endpoint=item.get("api_endpoint") or None,
                method=item.get("method") or None,
            ))
        return scenarios

    def _call_llm(self, user_prompt: str, system_prompt: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                return self._llm.chat_completion(
                    system=system_prompt,
                    user=user_prompt,
                    temperature=0.3,
                    max_tokens=8192,
                    response_format={"type": "json_object"},
                )
            except LLMTransientError as exc:
                last_exc = exc
                logger.warning("LLM transient error (attempt %d/3): %s", attempt, exc)
            except ValueError as exc:
                last_exc = exc
                logger.warning("LLM response error (attempt %d/3): %s", attempt, exc)
        raise ScenarioGenerationError(
            f"LLM unavailable after 3 attempts: {last_exc}"
        ) from last_exc
