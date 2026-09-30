"""Build a CSV report from generated test scenarios."""

from __future__ import annotations

import csv
import io

from app.models.schemas import GeneratedScenariosSchema, GeneratedScriptsSchema, TestScenarioType


def build_scenarios_csv(data: GeneratedScenariosSchema) -> bytes:
    """Return UTF-8 CSV bytes for the given scenarios."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)

    writer.writerow([
        "No.",
        "Name",
        "Type",
        "Description",
        "Steps",
        "Expected Result",
        "API Endpoint",
        "Method",
    ])

    for i, s in enumerate(data.scenarios, start=1):
        writer.writerow([
            i,
            s.name,
            s.type.value.capitalize(),
            s.description,
            " | ".join(f"{j}. {step}" for j, step in enumerate(s.steps, 1)),
            s.expected_result,
            s.api_endpoint or "",
            s.method or "",
        ])

    return buf.getvalue().encode("utf-8")


def build_comment_adf(data: GeneratedScenariosSchema, attachment_filename: str) -> dict:
    """Build an Atlassian Document Format (ADF) comment body."""
    positive = sum(1 for s in data.scenarios if s.type == TestScenarioType.POSITIVE)
    negative = sum(1 for s in data.scenarios if s.type == TestScenarioType.NEGATIVE)
    edge = sum(1 for s in data.scenarios if s.type == TestScenarioType.EDGE)

    def _heading(level: int, text: str) -> dict:
        return {"type": "heading", "attrs": {"level": level},
                "content": [{"type": "text", "text": text}]}

    def _para(text: str) -> dict:
        return {"type": "paragraph", "content": [{"type": "text", "text": text}]}

    def _bold_para(label: str, value: str) -> dict:
        return {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": label, "marks": [{"type": "strong"}]},
                {"type": "text", "text": value},
            ],
        }

    rows = []
    for i, s in enumerate(data.scenarios, start=1):
        rows.append({
            "type": "tableRow",
            "content": [
                {"type": "tableCell", "attrs": {}, "content": [_para(str(i))]},
                {"type": "tableCell", "attrs": {}, "content": [_para(s.name)]},
                {"type": "tableCell", "attrs": {}, "content": [_para(s.type.value.capitalize())]},
                {"type": "tableCell", "attrs": {}, "content": [_para(s.description)]},
                {"type": "tableCell", "attrs": {}, "content": [_para(s.expected_result)]},
                {"type": "tableCell", "attrs": {}, "content": [
                    _para(f"{s.method} {s.api_endpoint}" if s.api_endpoint else "—")
                ]},
            ],
        })

    header_row = {
        "type": "tableRow",
        "content": [
            {"type": "tableHeader", "attrs": {}, "content": [_para("#")]},
            {"type": "tableHeader", "attrs": {}, "content": [_para("Scenario Name")]},
            {"type": "tableHeader", "attrs": {}, "content": [_para("Type")]},
            {"type": "tableHeader", "attrs": {}, "content": [_para("Description")]},
            {"type": "tableHeader", "attrs": {}, "content": [_para("Expected Result")]},
            {"type": "tableHeader", "attrs": {}, "content": [_para("API")]},
        ],
    }

    return {
        "type": "doc",
        "version": 1,
        "content": [
            _heading(2, "🤖 AI-Generated Test Scenarios"),
            _para(f"Auto-generated for ticket {data.ticket_id}: {data.summary}"),
            _bold_para("Total: ", f"{len(data.scenarios)} scenarios"),
            _bold_para("Positive: ", str(positive)),
            _bold_para("Negative: ", str(negative)),
            _bold_para("Edge: ", str(edge)),
            _para(f"Full CSV report attached as: {attachment_filename}"),
            {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
             "content": [header_row, *rows]},
        ],
    }


def build_scripts_comment_adf(data: GeneratedScriptsSchema) -> dict:
    """Build an ADF comment summarising generated Pytest scripts."""
    def _heading(level: int, text: str) -> dict:
        return {"type": "heading", "attrs": {"level": level},
                "content": [{"type": "text", "text": text}]}

    def _para(text: str) -> dict:
        return {"type": "paragraph", "content": [{"type": "text", "text": text}]}

    file_lines = [f"• {f.filename} (attached)" for f in data.files]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            _heading(2, "🧪 AI-Generated Pytest Automation Scripts"),
            _para(f"Auto-generated for ticket {data.ticket_id}: {data.summary}"),
            _para(f"Output directory: {data.output_dir}"),
            _para("Generated files:"),
            *[_para(line) for line in file_lines],
            _para("Run locally: pytest output/" + data.ticket_id + " -v"),
        ],
    }
