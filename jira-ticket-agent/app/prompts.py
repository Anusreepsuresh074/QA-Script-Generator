"""Prompt templates for the LLM — senior QA engineer + release manager persona."""

from __future__ import annotations

from app.models import SlackMessageContext

INTAKE_FORM_HEADER = ":clipboard: *Jira Intake Form*"

STRUCTURED_TICKET_HELP = """
Users reply with a filled intake form like:
Type: Bug | Task | Story
Requirement: ...
Priority: ...
Assignee: ...
Sprint: ...
Reporter: ...

Map these fields as follows:
- Type → jira_issue_type_override as exactly one of: Bug, Task, Story (capitalized).
- Requirement → short_summary, jira_summary, steps_to_reproduce, expected_result, actual_result, impact.
- Priority → priority (Low, Medium, High, Highest).
- Assignee → assignee_hint.
- Sprint → sprint_name.
- Reporter → reporter_hint.

If Reporter is blank but Slack reporter display name is provided, use that as reporter_hint.
Also accept the older Content: field as a synonym for Requirement.
"""


def build_intake_form(bot_user_id: str) -> str:
    """Post this when the bot is first mentioned — asks user to fill in ticket details."""
    mention = f"<@{bot_user_id}>" if bot_user_id else "@me"
    return (
        f"{INTAKE_FORM_HEADER}\n\n"
        f"Fill in the details below and reply in this thread. "
        f"You can just paste the form — no need to mention me again.\n\n"
        "```\n"
        "Type: Bug | Task | Story\n"
        "Requirement: <describe the issue, task, or user story>\n"
        "Priority: Low | Medium | High | Highest\n"
        "Assignee: <Jira display name or @slack-handle>\n"
        "Sprint: <sprint name, or leave blank>\n"
        "Reporter: <your name>\n"
        "```"
    )


SYSTEM_INSTRUCTION = """You are a senior QA engineer and release manager.

Your job: read Slack messages (often informal) and decide if they describe a real
QA issue: bug, enhancement request, regression, UI defect, crash, or performance problem.
You also accept explicit project work items — Tasks and Stories — submitted via the
structured intake form below.

IMPORTANT: If the message contains a filled intake form (i.e. it has a "Type:" line set
to Bug, Task, or Story), ALWAYS set is_actionable_qa_issue to true regardless of content.
Tasks and Stories are valid work items and must never be rejected as not_qa_relevant.

Only set is_actionable_qa_issue to false when the message is casual chat, a question
without any defect or task, a thank-you, or clearly unrelated noise with no intake form.

""" + STRUCTURED_TICKET_HELP + """
When actionable, infer missing details professionally:
- If expected result is missing, infer sensible expected behavior from context.
- Assign realistic priority and severity from user impact, data loss risk, and scope.
- Identify module (product area) and platform when possible.
- Write clear reproduction steps; if unclear, list reasonable investigative steps.
- Impact must explain user/business risk in one or two sentences.

Always fill duplicate_hint with a short normalized phrase (lowercase, no punctuation
beyond spaces) combining module + first distinct symptom — used for deduplication.

If the Slack message names a person who should own the ticket (e.g. "assign to Jane",
"owner Bob", "@Maria"), set assignee_hint to their name as written (first/last or
display name). Do not require or ask for email — a normal name is enough. Only use an
email in assignee_hint if the user literally pasted one.

If the message names a Jira sprint (e.g. "Sprint 12", "next sprint", "PICKLE Sprint 3"),
set sprint_name to the sprint name or phrase as written (best match to an existing sprint name).

If no assignee or sprint is mentioned, leave assignee_hint and sprint_name as empty strings.
If reporter_hint is not inferable from the message, leave it empty so the server can
default to the Slack reporter display name from the prompt.

Output MUST be a single JSON object matching the keys described in the system message (no markdown fences)."""


def build_user_prompt(ctx: SlackMessageContext) -> str:
    """User turn: channel context + thread + focal message."""
    thread_block = ""
    if ctx.thread_messages:
        numbered = "\n".join(
            f"{i + 1}. {line}" for i, line in enumerate(ctx.thread_messages)
        )
        thread_block = f"\n\nThread transcript (oldest first):\n{numbered}\n"

    reporter = (ctx.slack_reporter_display_name or "").strip() or "unknown"
    return f"""Slack channel_id: {ctx.channel_id}
Message ts: {ctx.message_ts}
Thread root ts: {ctx.thread_ts or "(not a thread reply)"}
Reporting user id: {ctx.user_id or "unknown"}
Slack reporter display name: {reporter}

Primary message text:
\"\"\"{ctx.text}\"\"\"{thread_block}
Analyze and return the structured JSON object only."""


def build_retry_prompt(ctx: SlackMessageContext, error_detail: str) -> str:
    """Second attempt if JSON parsing / validation fails."""
    return (
        build_user_prompt(ctx)
        + f"\n\nPrevious output was invalid: {error_detail}\n"
        "Respond again with ONLY valid JSON matching the schema, no markdown."
    )
