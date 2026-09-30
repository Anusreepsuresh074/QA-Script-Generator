"""Dashboard API routes – config, Jira tickets, and generated output browser."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from requests.auth import HTTPBasicAuth

from app.config.settings import get_settings
from app.github.pr_service import (
    CredentialError,
    PrOptions,
    PullRequestError,
    TicketSource,
    create_automation_pr,
    describe_target_repo,
    sanitize_branch,
    sanitize_repo_dir,
)
from app.github.repo_analyzer import RepoConventions
from app.github.test_selection import (
    TestCaseRef,
    TestSelectionError,
    parse_cases,
    resolve_test_ids,
)
from app.jira.jira_client import JiraAuthenticationError, JiraClient, JiraConnectionError
from app.utils.pytest_templates import build_pytest_ini
from app.utils.test_report_builder import (
    build_test_report_csv,
    build_test_report_html,
    build_test_report_json,
    build_test_report_xlsx,
    is_report_filename,
    report_filenames,
    utc_now_iso,
    utc_stamp_for_path,
)

logger = logging.getLogger(__name__)

# Hard ceiling on how long a generated suite may run before it is killed.
_PYTEST_TIMEOUT = 180

# Guard against one click opening dozens of PRs in the automation repo.
_MAX_INDIVIDUAL_PRS = 20

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class DashboardConfig(BaseModel):
    jira_url: str
    jira_project_key: str
    mapping_type: str
    ollama_model: str
    groq_model: str
    output_dir: str
    api_base_url: str
    auto_create_pr: bool = False
    github_automation_repo: str = ""
    github_pr_base_branch: str = "main"
    github_tests_path: str = "tests"
    github_analyze_repo: bool = True
    github_adapt_tests: bool = False
    github_pr_branch_prefix: str = "automation"


class OutputFileInfo(BaseModel):
    filename: str
    size_bytes: int


class OutputTicketInfo(BaseModel):
    ticket_id: str
    output_dir: str
    files: list[OutputFileInfo]
    has_latest_report: bool = False
    latest_report_dir: Optional[str] = None
    archived_report_count: int = 0


class JiraTicketListItem(BaseModel):
    key: str
    summary: str
    status: str
    issue_type: str


class FileContentResponse(BaseModel):
    ticket_id: str
    filename: str
    content: str


class PrScope(str, Enum):
    """Which tests a PR should carry."""

    ALL = "all"                # the whole generated suite
    PASSED = "passed"          # only the tests that passed in the last run
    INDIVIDUAL = "individual"  # legacy alias for grouping=per_test


class PrGrouping(str, Enum):
    """
    How the selected tests are split into pull requests.

    Independent of :class:`PrScope`, which decides *which* tests ship. One
    combined PR keeps a release together; per-ticket matches the old behaviour;
    per-test keeps each case independently reviewable and mergeable.
    """

    SINGLE = "single"          # one PR for everything selected, across tickets
    PER_TICKET = "per_ticket"  # one PR per ticket
    PER_TEST = "per_test"      # one PR per test case


class PrTestCase(BaseModel):
    """A test case from the run on screen, used to scope a PR."""

    name: str
    classname: str = ""
    outcome: str = "passed"


class PullRequestInfo(BaseModel):
    """One PR raised by a request."""

    label: str
    pr_url: str
    branch: str
    repo: str
    created: bool
    message: str
    scope: str
    file_count: int
    tests: list[str] = []
    notes: list[str] = []
    tickets: list[str] = []
    target_path: str = ""
    title: str = ""
    conventions: list[str] = []
    adapted_files: list[str] = []


class RunTestsRequest(BaseModel):
    api_base_url: Optional[str] = Field(
        None, description="Override API_BASE_URL for this run (else uses config default)"
    )
    api_key: Optional[str] = Field(
        None, description="Override API_KEY for this run (sent to the conftest fixture)"
    )
    create_pr: bool = Field(
        False,
        description=(
            "After the test run, push generated automation and open a PR "
            "in GITHUB_AUTOMATION_REPO. All generated tests are included "
            "regardless of outcome — passing tests are not required"
        ),
    )
    pr_scope: PrScope = Field(
        PrScope.ALL,
        description=(
            "Scope of the post-run PR: 'all' tests, 'passed' tests only, or "
            "'individual' for one PR per test"
        ),
    )
    pr_grouping: Optional[PrGrouping] = Field(
        None,
        description=(
            "How to split the post-run PR: 'single', 'per_ticket', or "
            "'per_test'. Omit to derive it from pr_scope"
        ),
    )
    pr_branch: Optional[str] = Field(
        None,
        description=(
            "Branch name for the PR; sanitised to a legal git ref. Omit for "
            "the auto-generated <prefix>/<TICKET> name"
        ),
    )
    pr_target_path: Optional[str] = Field(
        None,
        description=(
            "Directory in the automation repo to write tests into. Omit to use "
            "the test root discovered by repo analysis, else GITHUB_TESTS_PATH"
        ),
    )
    pr_title: Optional[str] = Field(
        None, description="Override the generated PR title"
    )
    pr_analyze_repo: Optional[bool] = Field(
        None,
        description=(
            "Read the target repo's layout, fixtures, and pytest config before "
            "raising the PR (defaults to GITHUB_ANALYZE_REPO)"
        ),
    )
    pr_adapt_tests: Optional[bool] = Field(
        None,
        description=(
            "Rewrite generated modules to reuse the target repo's fixtures and "
            "style (defaults to GITHUB_ADAPT_TESTS; needs pr_analyze_repo)"
        ),
    )


class CreatePrRequest(BaseModel):
    """
    Raise PR(s) for a ticket's generated tests without re-running the suite.

    The optional counters and ``cases`` let the UI scope the PR to the results
    already on screen; when omitted, the latest saved report is used instead.
    """

    scope: PrScope = Field(
        PrScope.ALL,
        description=(
            "Which tests ship: 'all' = the whole suite, 'passed' = only the "
            "passing tests, 'individual' = legacy alias for grouping=per_test"
        ),
    )
    grouping: Optional[PrGrouping] = Field(
        None,
        description=(
            "How the selection is split into PRs: 'single' = one combined PR "
            "(across every selected ticket), 'per_ticket' = one PR each, "
            "'per_test' = one PR per test. Omit to derive from scope and the "
            "number of tickets"
        ),
    )
    tickets: Optional[list[str]] = Field(
        None,
        description=(
            "Additional ticket ids to include alongside the path ticket — the "
            "way to combine several tickets into one PR"
        ),
    )
    branch: Optional[str] = Field(
        None,
        description=(
            "Branch name to raise the PR from; sanitised to a legal git ref. "
            "Omit for the auto-generated <prefix>/<TICKET> name. With per-test "
            "or per-ticket grouping a distinguishing suffix is appended so the "
            "PRs stay separate"
        ),
    )
    target_path: Optional[str] = Field(
        None,
        description=(
            "Directory in the automation repo to write tests into (e.g. "
            "'qa/tests/api'). Omit to use the test root found by repo analysis, "
            "else GITHUB_TESTS_PATH"
        ),
    )
    title: Optional[str] = Field(
        None, description="Override the generated PR title"
    )
    analyze_repo: Optional[bool] = Field(
        None,
        description=(
            "Read the target repo before raising the PR — its test layout, "
            "naming, conftest fixtures, and pytest config — so files land where "
            "its own tests live (defaults to GITHUB_ANALYZE_REPO)"
        ),
    )
    adapt_tests: Optional[bool] = Field(
        None,
        description=(
            "Rewrite the generated modules to reuse the target repo's fixtures, "
            "helpers, and style before pushing. Rejected rewrites fall back to "
            "the file as generated (defaults to GITHUB_ADAPT_TESTS)"
        ),
    )
    refresh_analysis: bool = Field(
        False, description="Ignore the cached repo analysis and re-read the repo"
    )
    tests: Optional[list[str]] = Field(
        None,
        description=(
            "Restrict per-test grouping to these test ids (module::test or "
            "bare test name); omit to raise one PR for every recorded test"
        ),
    )
    cases: Optional[list[PrTestCase]] = Field(
        None,
        description="Per-test outcomes from the run on screen; falls back to the saved report",
    )
    total: Optional[int] = None
    passed: Optional[int] = None
    failed: Optional[int] = None
    errors: Optional[int] = None
    skipped: Optional[int] = None
    duration: Optional[float] = None
    base_url: Optional[str] = None


class CreatePrResponse(BaseModel):
    ticket_id: str
    scope: str
    test_summary: str
    prs: list[PullRequestInfo]
    failures: list[str] = []
    grouping: str = PrGrouping.SINGLE.value
    tickets: list[str] = []
    target_path: str = ""
    conventions: list[str] = []
    skipped: list[str] = []
    # Convenience mirrors of prs[0] so single-PR callers stay simple
    pr_url: Optional[str] = None
    branch: Optional[str] = None
    repo: Optional[str] = None
    created: Optional[bool] = None
    pr_message: Optional[str] = None


class TestCaseResult(BaseModel):
    name: str
    classname: str
    outcome: str  # passed | failed | error | skipped
    duration: float
    message: Optional[str] = None


class RunTestsResponse(BaseModel):
    ticket_id: str
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    duration: float
    exit_code: int
    base_url: str
    cases: list[TestCaseResult]
    output: str
    pr_url: Optional[str] = None
    pr_message: Optional[str] = None
    prs: list[PullRequestInfo] = []


class TestReportResponse(RunTestsResponse):
    generated_at: str
    report_files: dict[str, str]


@router.get("/config", response_model=DashboardConfig)
async def dashboard_config() -> DashboardConfig:
    """Return non-secret configuration for the UI."""
    s = get_settings()
    return DashboardConfig(
        jira_url=s.jira_url.rstrip("/"),
        jira_project_key=s.jira_project_key,
        mapping_type=s.mapping_type.value,
        ollama_model=s.ollama_model,
        groq_model=s.groq_model,
        output_dir=s.output_dir,
        api_base_url=s.api_base_url,
        auto_create_pr=s.auto_create_pr,
        github_automation_repo=s.github_automation_repo,
        github_pr_base_branch=s.github_pr_base_branch,
        github_tests_path=s.github_tests_path,
        github_analyze_repo=s.github_analyze_repo,
        github_adapt_tests=s.github_adapt_tests,
        github_pr_branch_prefix=s.github_pr_branch_prefix,
    )


def _reports_ticket_root(ticket_id: str) -> Path:
    return Path(get_settings().reports_dir) / ticket_id


def _latest_report_dir(ticket_id: str) -> Path:
    return _reports_ticket_root(ticket_id) / "latest"


def _latest_report_available(ticket_id: str) -> bool:
    latest = _latest_report_dir(ticket_id)
    names = report_filenames(ticket_id)
    return any((latest / fname).is_file() for fname in names.values())


def _count_archived_reports(ticket_id: str) -> int:
    root = _reports_ticket_root(ticket_id)
    if not root.is_dir():
        return 0
    return sum(
        1
        for path in root.iterdir()
        if path.is_dir() and path.name != "latest"
    )


def _archive_latest_reports(ticket_id: str) -> Optional[Path]:
    """
    Move the current latest report set into reports/<ticket>/<timestamp>/.

    Also migrates any legacy report files left under output/<ticket>/.
    """
    settings = get_settings()
    ticket_root = _reports_ticket_root(ticket_id)
    latest_dir = ticket_root / "latest"
    names = report_filenames(ticket_id)
    stamp = utc_stamp_for_path()
    archive_dir = ticket_root / stamp

    legacy_dir = Path(settings.output_dir) / ticket_id
    legacy_files = [
        legacy_dir / fname
        for fname in names.values()
        if (legacy_dir / fname).is_file()
    ]
    latest_files = [
        latest_dir / fname
        for fname in names.values()
        if (latest_dir / fname).is_file()
    ]

    to_archive = latest_files or legacy_files
    if not to_archive:
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    for src in to_archive:
        dest = archive_dir / src.name
        shutil.move(str(src), str(dest))
        logger.info("Archived report %s → %s", src, dest)

    # Clean empty latest dir before rewrite
    if latest_dir.is_dir() and not any(latest_dir.iterdir()):
        latest_dir.rmdir()

    return archive_dir


def _save_test_reports(ticket_dir: Path, result: RunTestsResponse) -> tuple[str, dict[str, str]]:
    """
    Write the latest report set under reports/<ticket>/latest/.

    Any previous latest (or legacy output/) reports are moved to
    reports/<ticket>/<timestamp>/ so the dashboard only exposes the newest run.
    """
    ticket_id = result.ticket_id
    archived = _archive_latest_reports(ticket_id)
    if archived:
        logger.info("Previous reports for %s archived to %s", ticket_id, archived)

    generated_at = utc_now_iso()
    payload = result.model_dump()
    payload["generated_at"] = generated_at

    names = report_filenames(ticket_id)
    latest_dir = _latest_report_dir(ticket_id)
    latest_dir.mkdir(parents=True, exist_ok=True)

    (latest_dir / names["html"]).write_text(build_test_report_html(payload), encoding="utf-8")
    (latest_dir / names["json"]).write_text(build_test_report_json(payload), encoding="utf-8")
    (latest_dir / names["csv"]).write_text(
        build_test_report_csv([c.model_dump() for c in result.cases]),
        encoding="utf-8",
    )
    (latest_dir / names["xlsx"]).write_bytes(build_test_report_xlsx(payload))

    # Remove any leftover report copies from the script output folder
    for fname in names.values():
        leftover = ticket_dir / fname
        if leftover.is_file():
            leftover.unlink()

    logger.info("Saved latest test reports for %s in %s", ticket_id, latest_dir)
    return generated_at, names


@router.get("/outputs", response_model=list[OutputTicketInfo])
async def list_outputs() -> list[OutputTicketInfo]:
    """
    Return only the most recent generated ticket output.

    Historical ticket folders remain on disk under output/, but the dashboard
    Generated outputs section shows the last run only (scripts + actions).
    Report history stays under reports/ and is not listed here.
    """
    root = Path(get_settings().output_dir)
    if not root.exists():
        return []

    ticket_dirs = [p for p in root.iterdir() if p.is_dir()]
    if not ticket_dirs:
        return []

    # Most recently modified ticket folder = last pipeline / test run
    ticket_dir = max(ticket_dirs, key=lambda p: p.stat().st_mtime)
    files = [
        OutputFileInfo(filename=f.name, size_bytes=f.stat().st_size)
        for f in sorted(ticket_dir.iterdir())
        if f.is_file() and not is_report_filename(f.name) and not f.name.startswith(".")
    ]
    return [
        OutputTicketInfo(
            ticket_id=ticket_dir.name,
            output_dir=str(ticket_dir),
            files=files,
            has_latest_report=_latest_report_available(ticket_dir.name),
            latest_report_dir=str(_latest_report_dir(ticket_dir.name))
            if _latest_report_available(ticket_dir.name)
            else None,
            archived_report_count=0,  # do not surface archive history in the UI
        )
    ]


@router.get("/outputs/{ticket_id}/{filename}", response_model=FileContentResponse)
async def get_output_file(ticket_id: str, filename: str) -> FileContentResponse:
    """Return the contents of a generated file for preview."""
    if ".." in ticket_id or ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid path")

    path = Path(get_settings().output_dir) / ticket_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileContentResponse(
        ticket_id=ticket_id,
        filename=filename,
        content=path.read_text(encoding="utf-8"),
    )


def _parse_junit(xml_path: Path) -> tuple[list[TestCaseResult], float]:
    """Parse a pytest JUnit XML report into structured per-test results."""
    cases: list[TestCaseResult] = []
    total_duration = 0.0

    tree = ET.parse(xml_path)
    root = tree.getroot()
    suites = root.findall("testsuite") or ([root] if root.tag == "testsuite" else [])

    for suite in suites:
        total_duration += float(suite.get("time", 0) or 0)
        for tc in suite.findall("testcase"):
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            if error is not None:
                outcome, node = "error", error
            elif failure is not None:
                outcome, node = "failed", failure
            elif skipped is not None:
                outcome, node = "skipped", skipped
            else:
                outcome, node = "passed", None

            message: Optional[str] = None
            if node is not None:
                message = node.get("message") or (node.text or "").strip() or None

            cases.append(
                TestCaseResult(
                    name=tc.get("name", "?"),
                    classname=tc.get("classname", ""),
                    outcome=outcome,
                    duration=float(tc.get("time", 0) or 0),
                    message=message,
                )
            )

    return cases, total_duration


def _summary_from_counts(counts: dict[str, Any]) -> str:
    """One-line test summary for a PR body, built from run counters."""
    total = int(counts.get("total") or 0)
    passed = int(counts.get("passed") or 0)
    failed = int(counts.get("failed") or 0) + int(counts.get("errors") or 0)
    skipped = int(counts.get("skipped") or 0)
    duration = float(counts.get("duration") or 0)
    base_url = counts.get("base_url") or "unset"

    if total == 0:
        return f"No tests were collected in the last run (base_url={base_url})"

    parts = [f"{passed}/{total} passed"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    stamp = counts.get("generated_at")
    return (
        f"{', '.join(parts)} in {round(duration, 2)}s (base_url={base_url})"
        f"{f' — run at {stamp}' if stamp else ''}"
    )


def _validate_ticket_id(ticket_id: str) -> str:
    """Reject anything that could escape the output directory."""
    if not ticket_id or ".." in ticket_id or "/" in ticket_id or "\\" in ticket_id:
        raise HTTPException(status_code=400, detail=f"Invalid ticket id {ticket_id!r}")
    return ticket_id


def _output_dir_for(ticket_id: str) -> Path:
    return Path(get_settings().output_dir) / ticket_id


def _pr_options(
    *,
    summary: str,
    trigger: str,
    branch: Optional[str] = None,
    target_path: Optional[str] = None,
    title: Optional[str] = None,
    analyze_repo: Optional[bool] = None,
    adapt_tests: Optional[bool] = None,
    refresh_analysis: bool = False,
) -> PrOptions:
    """
    Merge the request's PR controls with the configured defaults.

    Every field is user-controllable from the dashboard; ``None`` means "not
    specified", which falls back to settings. Adaptation implies analysis —
    rewriting tests against conventions nobody read would be guesswork.
    """
    cfg = get_settings()
    analyze = cfg.github_analyze_repo if analyze_repo is None else analyze_repo
    adapt = cfg.github_adapt_tests if adapt_tests is None else adapt_tests

    if branch:
        cleaned = sanitize_branch(branch)
        if not cleaned:
            raise HTTPException(
                status_code=422,
                detail=f"Branch name {branch!r} has no usable characters for a git ref",
            )
        branch = cleaned
    if target_path is not None and target_path.strip():
        try:
            target_path = sanitize_repo_dir(target_path)
        except PullRequestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return PrOptions(
        branch=branch or None,
        target_path=target_path,
        title=(title or "").strip() or None,
        analyze_repo=bool(analyze or adapt),
        adapt_tests=bool(adapt),
        test_summary=summary,
        trigger=trigger,
        refresh_analysis=refresh_analysis,
    )


def _resolve_grouping(
    scope: PrScope,
    requested: Optional[PrGrouping],
    ticket_count: int,
) -> PrGrouping:
    """
    The grouping to use when the request did not name one.

    Keeps older callers behaving as before: ``scope=individual`` still means one
    PR per test, and a single ticket still gets a single PR.
    """
    if requested is not None:
        return requested
    if scope is PrScope.INDIVIDUAL:
        return PrGrouping.PER_TEST
    return PrGrouping.PER_TICKET if ticket_count > 1 else PrGrouping.SINGLE


def _filter_by_scope(cases: list[TestCaseRef], scope: PrScope) -> list[TestCaseRef]:
    return [c for c in cases if c.passed] if scope is PrScope.PASSED else list(cases)


def _expand_targets(
    sources: list[TicketSource],
    grouping: PrGrouping,
    scope: PrScope,
) -> list[tuple[str, list[TicketSource]]]:
    """
    One ``(pr_scope, sources)`` entry per pull request to raise.

    This is where grouping becomes concrete: ``single`` hands every ticket to
    one PR, ``per_ticket`` one PR each, and ``per_test`` fans out to a PR per
    case. ``pr_scope`` is what :func:`create_automation_pr` slices modules by.
    """
    pr_scope = "passed" if scope is PrScope.PASSED else "all"
    if grouping is PrGrouping.PER_TEST:
        return [
            ("test", [TicketSource(src.ticket_id, src.directory, [case])])
            for src in sources
            for case in src.cases
        ]
    if grouping is PrGrouping.PER_TICKET:
        return [(pr_scope, [src]) for src in sources]
    return [(pr_scope, list(sources))]


def _branch_for_target(
    options: PrOptions,
    grouping: PrGrouping,
    sources: list[TicketSource],
    total_targets: int,
) -> Optional[str]:
    """
    Keep a user-supplied branch unique across a fan-out.

    One PR uses the typed name verbatim. Per-test PRs get the test slug appended
    downstream, so the name passes through. Per-ticket PRs would otherwise all
    target the same branch and overwrite each other, so the ticket is appended.
    """
    if not options.branch:
        return None
    if total_targets <= 1 or grouping is not PrGrouping.PER_TICKET:
        return options.branch
    return f"{options.branch}-{sources[0].safe_id}"


def _target_label(pr_scope: str, sources: list[TicketSource]) -> str:
    if pr_scope == "test" and sources and sources[0].cases:
        case = sources[0].cases[0]
        return case.test_id or case.func_name
    return ", ".join(src.ticket_id for src in sources) or pr_scope


def _raise_prs(
    targets: list[tuple[str, list[TicketSource]]],
    options: PrOptions,
    grouping: PrGrouping,
) -> tuple[list[PullRequestInfo], list[str]]:
    """
    Raise every planned PR, collecting per-target failures instead of aborting.

    A rejected credential is the exception: it will fail identically for every
    remaining target, so the fan-out stops rather than replaying the same error
    twenty times.
    """
    infos: list[PullRequestInfo] = []
    failures: list[str] = []

    for pr_scope, sources in targets:
        label = _target_label(pr_scope, sources)
        opts = replace(
            options,
            branch=_branch_for_target(options, grouping, sources, len(targets)),
        )
        try:
            pr = create_automation_pr(sources, scope=pr_scope, options=opts)
        except CredentialError as exc:
            failures.append(str(exc))
            logger.error("GitHub credential rejected while raising PRs: %s", exc)
            remaining = len(targets) - len(infos) - len(failures)
            if remaining > 0:
                failures.append(
                    f"{remaining} further PR(s) skipped — the same credential "
                    "would be rejected"
                )
            break
        except PullRequestError as exc:
            failures.append(f"{label}: {exc}")
            logger.warning("PR creation failed for %s: %s", label, exc)
            continue

        infos.append(_to_pr_info(pr))
        logger.info("Automation PR for %s (%s): %s", label, pr.label, pr.url)

    return infos, failures


def _maybe_create_pr(
    ticket_id: str,
    ticket_dir: Path,
    result: RunTestsResponse,
    body: RunTestsRequest | None,
) -> RunTestsResponse:
    """
    Open automation PR(s) after a run when the request asked for them.

    Scope decides which tests ship (all, or only those that passed) and grouping
    decides how they are split (one PR, or one per test). The real run summary
    goes into the PR body either way, and PR failures never fail the run.
    """
    if not body or not body.create_pr:
        return result

    scope = body.pr_scope
    grouping = _resolve_grouping(scope, body.pr_grouping, 1)
    summary = _summary_from_counts(result.model_dump())
    cases = parse_cases([c.model_dump() for c in result.cases])

    needs_cases = scope is PrScope.PASSED or grouping is PrGrouping.PER_TEST
    if needs_cases and not cases:
        result.pr_message = (
            f"PR skipped — {'scope ' + scope.value if scope is PrScope.PASSED else grouping.value} "
            "needs per-test results but the run reported none"
        )
        logger.info("%s: %s", ticket_id, result.pr_message)
        return result

    selected = _filter_by_scope(cases, scope)
    if scope is PrScope.PASSED and not selected:
        result.pr_message = (
            "PR skipped — no passing tests in this run to scope a passing-only PR to"
        )
        logger.info("%s: %s", ticket_id, result.pr_message)
        return result

    sources = [TicketSource(ticket_id, ticket_dir, selected)]
    targets = _expand_targets(sources, grouping, scope)
    if grouping is PrGrouping.PER_TEST and len(targets) > _MAX_INDIVIDUAL_PRS:
        result.pr_message = (
            f"PR skipped — {len(targets)} tests exceeds the {_MAX_INDIVIDUAL_PRS} "
            "per-test PR limit; raise them from the results panel instead"
        )
        logger.info("%s: %s", ticket_id, result.pr_message)
        return result

    try:
        options = _pr_options(
            summary=summary,
            trigger="automatically after the test run",
            branch=body.pr_branch,
            target_path=body.pr_target_path,
            title=body.pr_title,
            analyze_repo=body.pr_analyze_repo,
            adapt_tests=body.pr_adapt_tests,
        )
    except HTTPException as exc:
        result.pr_message = f"PR skipped — {exc.detail}"
        logger.info("%s: %s", ticket_id, result.pr_message)
        return result

    infos, failures = _raise_prs(targets, options, grouping)
    result.prs.extend(infos)

    if infos:
        result.pr_url = infos[0].pr_url
        result.pr_message = (
            infos[0].message
            if len(infos) == 1
            else f"{len(infos)} PR(s) raised for {ticket_id}"
        )
        if failures:
            result.pr_message += f"; {len(failures)} failed"
    else:
        result.pr_message = f"PR creation failed: {'; '.join(failures)}"
    return result


def _execute_pytest_suite(
    ticket_id: str,
    body: RunTestsRequest | None = None,
) -> RunTestsResponse:
    """Run pytest for a ticket output folder and return structured results."""
    if ".." in ticket_id or "/" in ticket_id:
        raise HTTPException(status_code=400, detail="Invalid ticket id")

    settings = get_settings()
    ticket_dir = Path(settings.output_dir) / ticket_id
    if not ticket_dir.is_dir():
        raise HTTPException(status_code=404, detail="Output not found")

    test_files = [
        p for p in ticket_dir.iterdir()
        if p.is_file() and p.name.startswith("test_") and p.suffix == ".py"
    ]
    if not test_files:
        raise HTTPException(status_code=404, detail="No test modules found for this ticket")

    ini_path = ticket_dir / "pytest.ini"
    if not ini_path.exists():
        ini_path.write_text(build_pytest_ini(), encoding="utf-8")

    body = body or RunTestsRequest()
    base_url = (body.api_base_url or settings.api_base_url).rstrip("/")
    resolved_api_key = (
        body.api_key if body.api_key is not None else (settings.api_key_default or "")
    ).strip()

    env = os.environ.copy()
    env["API_BASE_URL"] = base_url
    if resolved_api_key:
        env["API_KEY"] = resolved_api_key
        # Some generated suites / mock APIs (e.g. ReqRes) read these aliases
        env["X_API_KEY"] = resolved_api_key
        env["REQRES_API_KEY"] = resolved_api_key

    # Keep REST conftest auth wiring in sync with current API_KEY_DEFAULT for this run
    conftest_path = ticket_dir / "conftest.py"
    if conftest_path.is_file():
        existing = conftest_path.read_text(encoding="utf-8")
        if "Shared REST fixtures" in existing and "Generated by QA Script Generator" in existing:
            from app.models.schemas import APIType
            from app.utils.pytest_templates import build_conftest

            conftest_path.write_text(
                build_conftest(
                    ticket_id,
                    base_url,
                    resolved_api_key,
                    api_type=APIType.REST,
                ),
                encoding="utf-8",
            )

    report_path = ticket_dir / ".pytest_report.xml"
    cmd = [
        sys.executable, "-m", "pytest", ".",
        "-p", "no:cacheprovider",
        f"--junit-xml={report_path.name}",
    ]

    logger.info(
        "Running pytest for %s (base_url=%s, api_key=%s)",
        ticket_id,
        base_url,
        "set" if resolved_api_key else "not set",
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ticket_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=_PYTEST_TIMEOUT,
        )
        output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        report_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=504,
            detail=f"Test run exceeded {_PYTEST_TIMEOUT}s and was terminated",
        )

    cases: list[TestCaseResult] = []
    duration = 0.0
    if report_path.exists():
        try:
            cases, duration = _parse_junit(report_path)
        except ET.ParseError as exc:
            logger.warning("Could not parse JUnit report for %s: %s", ticket_id, exc)
        finally:
            report_path.unlink(missing_ok=True)

    if len(output) > 20000:
        output = output[:20000] + "\n… (output truncated)"

    result = RunTestsResponse(
        ticket_id=ticket_id,
        total=len(cases),
        passed=sum(1 for c in cases if c.outcome == "passed"),
        failed=sum(1 for c in cases if c.outcome == "failed"),
        errors=sum(1 for c in cases if c.outcome == "error"),
        skipped=sum(1 for c in cases if c.outcome == "skipped"),
        duration=round(duration, 2),
        exit_code=exit_code,
        base_url=base_url,
        cases=cases,
        output=output.strip(),
    )
    return _maybe_create_pr(ticket_id, ticket_dir, result, body)


@router.post("/outputs/{ticket_id}/run", response_model=RunTestsResponse)
def run_output_tests(ticket_id: str, body: RunTestsRequest | None = None) -> RunTestsResponse:
    """Execute the generated Pytest suite for a ticket and return structured results."""
    return _execute_pytest_suite(ticket_id, body)


@router.post("/outputs/{ticket_id}/report", response_model=TestReportResponse)
def generate_test_report(ticket_id: str, body: RunTestsRequest | None = None) -> TestReportResponse:
    """Run tests, save latest reports under reports/<ticket>/latest/, archive prior runs."""
    result = _execute_pytest_suite(ticket_id, body)
    ticket_dir = Path(get_settings().output_dir) / ticket_id
    generated_at, report_files = _save_test_reports(ticket_dir, result)
    return TestReportResponse(
        **result.model_dump(),
        generated_at=generated_at,
        report_files=report_files,
    )


def _saved_report_counts(ticket_id: str) -> Optional[dict[str, Any]]:
    """Counters from the latest saved JSON report, if one exists."""
    names = report_filenames(ticket_id)
    candidates = [
        _latest_report_dir(ticket_id) / names["json"],
        Path(get_settings().output_dir) / ticket_id / names["json"],
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read saved report %s: %s", path, exc)
            continue
        if isinstance(data, dict):
            return data
    return None


def _resolve_pr_summary(ticket_id: str, body: CreatePrRequest) -> str:
    """Test summary for a manually raised PR: request counters, else last report."""
    if body.total is not None:
        return _summary_from_counts(body.model_dump())

    saved = _saved_report_counts(ticket_id)
    if saved:
        return _summary_from_counts(saved)

    return "No test run recorded — PR raised for the generated suite as-is"


def _resolve_pr_cases(ticket_id: str, body: CreatePrRequest) -> list[TestCaseRef]:
    """Per-test outcomes for scoping: request payload first, else saved report."""
    if body.cases:
        return parse_cases([c.model_dump() for c in body.cases])

    saved = _saved_report_counts(ticket_id) or {}
    return parse_cases(saved.get("cases") or [])


def _to_pr_info(pr) -> PullRequestInfo:
    return PullRequestInfo(
        label=pr.label,
        pr_url=pr.url,
        branch=pr.branch,
        repo=pr.repo,
        created=pr.created,
        message=pr.message,
        scope=pr.scope,
        file_count=pr.file_count,
        tests=pr.tests,
        notes=pr.notes,
        tickets=pr.tickets,
        target_path=pr.target_path,
        title=pr.title,
        conventions=pr.conventions,
        adapted_files=pr.adapted_files,
    )


def _apply_test_filter(
    cases: list[TestCaseRef],
    wanted: Optional[list[str]],
    *,
    strict: bool,
) -> list[TestCaseRef]:
    """
    Narrow ``cases`` to the requested test ids.

    A single-ticket request is strict — an unknown or ambiguous id is a 422, so
    a typo does not silently raise the wrong PR. Across several tickets an id
    naturally will not exist in most of them, so membership filtering is used
    and "nothing matched anywhere" is caught by the caller.
    """
    if not wanted:
        return cases
    if strict:
        return resolve_test_ids(cases, wanted)
    keep = set(wanted)
    return [c for c in cases if c.test_id in keep or c.func_name in keep]


@router.post("/outputs/{ticket_id}/pr", response_model=CreatePrResponse)
def create_output_pr(
    ticket_id: str,
    body: CreatePrRequest | None = None,
) -> CreatePrResponse:
    """
    Raise PR(s) for generated tests on demand, without re-running them.

    Backs the dashboard's "Raise PR" control, which exposes every knob: which
    tickets to include, which tests ship (all or passing only), how they are
    grouped into PRs (one combined, one per ticket, one per test), the branch
    name, where files land in the target repo, and whether to read that repo's
    conventions first and rewrite the tests to match.
    """
    _validate_ticket_id(ticket_id)
    body = body or CreatePrRequest()

    ticket_ids = [ticket_id]
    for extra in body.tickets or []:
        extra = (extra or "").strip()
        if extra and extra not in ticket_ids:
            _validate_ticket_id(extra)
            ticket_ids.append(extra)

    scope = body.scope
    grouping = _resolve_grouping(scope, body.grouping, len(ticket_ids))
    summary = _resolve_pr_summary(ticket_id, body)
    strict_filter = len(ticket_ids) == 1

    sources: list[TicketSource] = []
    skipped: list[str] = []

    for candidate in ticket_ids:
        directory = _output_dir_for(candidate)
        if not directory.is_dir():
            if candidate == ticket_id:
                raise HTTPException(status_code=404, detail="Output not found")
            skipped.append(f"{candidate}: no generated output on disk")
            continue

        # Counters and cases in the request describe the ticket on screen; the
        # other tickets fall back to their own latest saved report.
        cases = _resolve_pr_cases(
            candidate, body if candidate == ticket_id else CreatePrRequest()
        )
        try:
            cases = _apply_test_filter(cases, body.tests, strict=strict_filter)
        except TestSelectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        selected = _filter_by_scope(cases, scope)
        needs_cases = scope is PrScope.PASSED or grouping is PrGrouping.PER_TEST
        if needs_cases and not selected:
            reason = (
                "no passing tests in the last run"
                if scope is PrScope.PASSED
                else "no per-test results recorded — run the tests first"
            )
            skipped.append(f"{candidate}: {reason}")
            continue
        sources.append(TicketSource(candidate, directory, selected))

    if not sources:
        raise HTTPException(
            status_code=422,
            detail="; ".join(skipped)
            or (
                f"PR scope '{scope.value}' needs per-test results — run the tests "
                "for this ticket first, then raise the PR"
            ),
        )

    targets = _expand_targets(sources, grouping, scope)
    if not targets:
        raise HTTPException(status_code=422, detail="No tests selected for PRs")
    if grouping is PrGrouping.PER_TEST and len(targets) > _MAX_INDIVIDUAL_PRS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{len(targets)} tests selected but at most {_MAX_INDIVIDUAL_PRS} "
                "per-test PRs can be raised at once — narrow the selection"
            ),
        )

    options = _pr_options(
        summary=summary,
        trigger="manually from the QA dashboard",
        branch=body.branch,
        target_path=body.target_path,
        title=body.title,
        analyze_repo=body.analyze_repo,
        adapt_tests=body.adapt_tests,
        refresh_analysis=body.refresh_analysis,
    )

    logger.info(
        "Raising %d %s PR(s) for %s (scope=%s, branch=%s, path=%s, analyze=%s, adapt=%s)",
        len(targets),
        grouping.value,
        ", ".join(s.ticket_id for s in sources),
        scope.value,
        options.branch or "auto",
        options.target_path or "auto",
        options.analyze_repo,
        options.adapt_tests,
    )

    infos, failures = _raise_prs(targets, options, grouping)
    if not infos:
        raise HTTPException(
            status_code=502,
            detail="; ".join(failures + skipped) or "No PRs could be raised",
        )

    first = infos[0]
    opened = sum(1 for i in infos if i.created)
    if len(infos) == 1:
        message = first.message
    else:
        message = (
            f"{opened} PR(s) opened, {len(infos) - opened} updated "
            f"across {len(infos)} PR(s)"
        )
    if failures:
        message += f"; {len(failures)} failed"

    return CreatePrResponse(
        ticket_id=ticket_id,
        scope=scope.value,
        grouping=grouping.value,
        tickets=[src.ticket_id for src in sources],
        target_path=first.target_path,
        conventions=first.conventions,
        test_summary=summary,
        prs=infos,
        failures=failures,
        skipped=skipped,
        pr_url=first.pr_url,
        branch=first.branch,
        repo=first.repo,
        created=first.created,
        pr_message=message,
    )


class PrTicketOption(BaseModel):
    """A ticket that has generated tests, offerable to the PR ticket picker."""

    ticket_id: str
    test_files: int
    has_report: bool = False


@router.get("/pr/tickets", response_model=list[PrTicketOption])
def list_pr_ticket_options() -> list[PrTicketOption]:
    """
    Every ticket on disk with generated tests, most recent first.

    Distinct from ``GET /outputs``, which deliberately shows only the latest run
    in the "Generated outputs" panel. Combining several tickets into one PR
    needs the full list, so the PR controls read this instead.
    """
    root = Path(get_settings().output_dir)
    if not root.exists():
        return []

    options: list[tuple[float, PrTicketOption]] = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        test_files = [
            f
            for f in directory.iterdir()
            if f.is_file()
            and f.name.startswith("test_")
            and f.suffix == ".py"
            and not is_report_filename(f.name)
        ]
        if not test_files:
            continue
        options.append(
            (
                directory.stat().st_mtime,
                PrTicketOption(
                    ticket_id=directory.name,
                    test_files=len(test_files),
                    has_report=_latest_report_available(directory.name),
                ),
            )
        )

    return [opt for _, opt in sorted(options, key=lambda pair: pair[0], reverse=True)]


class RepoConventionsResponse(BaseModel):
    """What the target automation repo looks like, for the PR controls to show."""

    repo: str
    ref: str
    analyzed: bool
    test_root: str = ""
    suggested_path: str = ""
    test_roots: list[str] = []
    naming_pattern: str = ""
    module_examples: list[str] = []
    conftest_paths: list[str] = []
    fixtures: list[str] = []
    helpers: list[str] = []
    config_files: list[str] = []
    markers: list[str] = []
    requirements: list[str] = []
    style: dict[str, str] = {}
    uses_test_classes: bool = False
    sample_module_path: str = ""
    test_file_count: int = 0
    summary: list[str] = []
    notes: list[str] = []


def _to_conventions_response(
    conventions: RepoConventions,
) -> RepoConventionsResponse:
    fallback = get_settings().github_tests_path or "tests"
    return RepoConventionsResponse(
        repo=conventions.repo,
        ref=conventions.ref,
        analyzed=conventions.analyzed,
        test_root=conventions.test_root,
        suggested_path=conventions.test_root or fallback,
        test_roots=conventions.test_roots,
        naming_pattern=conventions.naming_pattern,
        module_examples=conventions.module_examples,
        conftest_paths=conventions.conftest_paths,
        fixtures=[f.signature() for f in conventions.fixtures],
        helpers=conventions.helpers,
        config_files=conventions.config_files,
        markers=conventions.markers,
        requirements=conventions.requirements,
        style=conventions.style,
        uses_test_classes=conventions.uses_test_classes,
        sample_module_path=conventions.sample_module_path,
        test_file_count=conventions.test_file_count,
        summary=conventions.summary_lines(),
        notes=conventions.notes,
    )


@router.get("/github/conventions", response_model=RepoConventionsResponse)
def github_repo_conventions(
    refresh: bool = Query(False, description="Re-read the repo, ignoring the cache"),
) -> RepoConventionsResponse:
    """
    Analyze the configured automation repo and report how it writes tests.

    The dashboard calls this to pre-fill the target path and to show reviewers
    which fixtures and helpers already exist before a PR is raised.
    """
    try:
        conventions = describe_target_repo(refresh=refresh)
    except CredentialError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PullRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_conventions_response(conventions)


@router.get("/outputs/{ticket_id}/report/download")
def download_test_report(
    ticket_id: str,
    format: str = Query("html", pattern="^(html|json|csv|xlsx)$"),
):
    """Download the latest generated test report for a ticket."""
    if ".." in ticket_id or "/" in ticket_id:
        raise HTTPException(status_code=400, detail="Invalid ticket id")

    names = report_filenames(ticket_id)
    if format not in names:
        raise HTTPException(status_code=400, detail="Invalid report format")

    path = _latest_report_dir(ticket_id) / names[format]
    if not path.is_file():
        # Fallback for reports generated before the reports/ layout change
        legacy = Path(get_settings().output_dir) / ticket_id / names[format]
        path = legacy if legacy.is_file() else path
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Report not found — generate it first using 'Generate report'",
        )

    media = {
        "html": "text/html",
        "json": "application/json",
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return FileResponse(
        path,
        media_type=media[format],
        filename=path.name,
    )


@router.get("/outputs/{ticket_id}/report/status")
def test_report_status(ticket_id: str) -> dict[str, Any]:
    """Return whether the latest saved test reports exist for a ticket."""
    if ".." in ticket_id or "/" in ticket_id:
        raise HTTPException(status_code=400, detail="Invalid ticket id")

    names = report_filenames(ticket_id)
    latest = _latest_report_dir(ticket_id)
    available = {fmt: (latest / fname).is_file() for fmt, fname in names.items()}
    if not any(available.values()):
        legacy = Path(get_settings().output_dir) / ticket_id
        available = {fmt: (legacy / fname).is_file() for fmt, fname in names.items()}
    return {
        "ticket_id": ticket_id,
        "available": available,
        "latest_dir": str(latest),
        "archived_count": _count_archived_reports(ticket_id),
    }


@router.get("/tickets", response_model=list[JiraTicketListItem])
async def list_jira_tickets(
    project: str | None = Query(None, description="Jira project key"),
    max_results: int = Query(20, ge=1, le=50),
) -> list[JiraTicketListItem]:
    """List recent Jira tickets for the workflow picker."""
    settings = get_settings()
    project_key = project or settings.jira_project_key
    base = settings.jira_url.rstrip("/")
    auth = HTTPBasicAuth(settings.jira_email, settings.jira_api_token)
    jql = f"project = {project_key} ORDER BY updated DESC"

    # Verify credentials before searching (search can return empty on bad auth).
    try:
        JiraClient(settings).connect()
    except JiraAuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Jira authentication failed — check JIRA_EMAIL and JIRA_API_TOKEN") from exc
    except JiraConnectionError as exc:
        raise HTTPException(status_code=502, detail=f"Jira unreachable: {exc}") from exc

    url = f"{base}/rest/api/3/search/jql"
    payload: dict[str, Any] = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ["summary", "status", "issuetype"],
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            auth=auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=settings.jira_request_timeout,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Jira unreachable: {exc}") from exc

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Jira authentication failed")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Jira search failed: {resp.text[:300]}")

    issues = resp.json().get("issues", [])
    items: list[JiraTicketListItem] = []
    for issue in issues:
        fields = issue.get("fields", {})
        items.append(
            JiraTicketListItem(
                key=issue.get("key", "?"),
                summary=fields.get("summary", ""),
                status=(fields.get("status") or {}).get("name", "?"),
                issue_type=(fields.get("issuetype") or {}).get("name", "?"),
            )
        )
    return items


@router.get("/health-detail")
async def health_detail() -> dict:
    """Extended health check including Jira connectivity."""
    result: dict[str, Any] = {"api": "ok", "jira": "unknown"}
    try:
        JiraClient().connect()
        result["jira"] = "ok"
    except JiraAuthenticationError:
        result["jira"] = "auth_failed"
    except JiraConnectionError as exc:
        result["jira"] = f"error: {exc}"
    except Exception as exc:
        logger.warning("Health detail Jira check failed: %s", exc)
        result["jira"] = "error"
    return result
