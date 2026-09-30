"""Create pull requests in the configured automation repository for generated tests."""

from __future__ import annotations

import base64
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import requests

from app.config.settings import Settings, get_settings
from app.github.repo_analyzer import RepoConventions, analyze_repo, credential_hint
from app.github.test_adapter import MAX_ADAPTED_MODULES, adapt_module
from app.github.test_selection import TestCaseRef, missing_tests, slice_module

logger = logging.getLogger(__name__)

_SKIP_NAMES = {
    ".pytest_report.xml",
    ".pytest_cache",
}
_SKIP_SUFFIXES = (
    "_test_report.html",
    "_test_report.json",
    "_test_report.csv",
    "_test_report.xlsx",
)

# Git ref characters that must never reach the GitHub API.
_BRANCH_ILLEGAL = re.compile(r"[^A-Za-z0-9._/-]+")
_MAX_BRANCH_LEN = 200


class PullRequestError(Exception):
    """Raised when automation PR creation fails."""


class CredentialError(PullRequestError):
    """
    Raised when the GitHub credential is rejected — expired, revoked, or
    missing the scope the automation repo needs.

    Separate from a plain :class:`PullRequestError` so the dashboard can say
    "your token expired, replace it" instead of surfacing a raw HTTP status.
    """



@dataclass
class TicketSource:
    """One ticket's generated output, and the cases a scoped PR should carry."""

    ticket_id: str
    directory: Path
    cases: list[TestCaseRef] = field(default_factory=list)

    @property
    def safe_id(self) -> str:
        return self.ticket_id.replace("/", "-")


@dataclass
class PrOptions:
    """Everything the dashboard lets a user decide about a PR."""

    branch: Optional[str] = None
    target_path: Optional[str] = None
    title: Optional[str] = None
    analyze_repo: bool = True
    adapt_tests: bool = False
    test_summary: str = ""
    trigger: str = "manually from the QA dashboard"
    refresh_analysis: bool = False


@dataclass
class PullRequestResult:
    """Outcome of an automation PR attempt."""

    url: str
    branch: str
    repo: str
    created: bool
    message: str
    scope: str = "all"
    label: str = "all tests"
    file_count: int = 0
    tests: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)
    target_path: str = ""
    title: str = ""
    conventions: list[str] = field(default_factory=list)
    adapted_files: list[str] = field(default_factory=list)


@dataclass
class _PrFile:
    """A file staged for the PR, tagged so adaptation knows what it is."""

    path: str
    content: str
    kind: str = ""  # "" | "test" | "conftest"


def _is_python_test_module(path: Path) -> bool:
    return path.suffix == ".py" and path.name.startswith("test_")


def _module_kind(path: Path) -> str:
    if path.name == "conftest.py":
        return "conftest"
    if _is_python_test_module(path):
        return "test"
    return ""


def _should_include(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name in _SKIP_NAMES or path.name.startswith("."):
        return False
    if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo", ".xlsx", ".xls"}:
        return False
    if any(path.name.endswith(suf) for suf in _SKIP_SUFFIXES):
        return False
    return True


def sanitize_branch(name: str) -> str:
    """
    Coerce a user-supplied branch name into a legal git ref.

    The dashboard lets people type the branch freely, so anything git would
    reject (spaces, ``..``, ``~^:?*``, leading/trailing separators, a ``.lock``
    suffix) is normalised rather than bounced back as an API error.
    """
    cleaned = _BRANCH_ILLEGAL.sub("-", (name or "").strip())
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"/{2,}", "/", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = "/".join(part.strip(".-") for part in cleaned.split("/") if part.strip(".-"))
    if cleaned.endswith(".lock"):
        cleaned = cleaned[: -len(".lock")]
    return cleaned[:_MAX_BRANCH_LEN].strip("/-.")


def sanitize_repo_dir(path: str) -> str:
    """Normalise a target directory in the automation repo (``""`` = repo root)."""
    cleaned = (path or "").strip().replace("\\", "/")
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise PullRequestError(
            f"Target path {path!r} may not contain '..' — give a path inside the repo"
        )
    return "/".join(parts)


def _repo_path(base: str, ticket_id: str, rel: str) -> str:
    segments = [s for s in (base, ticket_id.replace("/", "-"), rel) if s]
    return "/".join(segments)


def _collect_files(source: TicketSource, base: str) -> list[_PrFile]:
    files: list[_PrFile] = []
    for path in sorted(source.directory.rglob("*")):
        if not _should_include(path):
            continue
        rel = path.relative_to(source.directory).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("Skipping non-text file for PR: %s", path)
            continue
        files.append(
            _PrFile(
                path=_repo_path(base, source.ticket_id, rel),
                content=content,
                kind=_module_kind(path),
            )
        )
    return files


def _collect_selected_files(
    source: TicketSource,
    base: str,
    *,
    header_note: str = "",
    rename_module_to: Optional[str] = None,
) -> tuple[list[_PrFile], list[str]]:
    """
    Collect PR files with Python test modules sliced down to ``source.cases``.

    Support files (``conftest.py``, ``pytest.ini``, …) are always included so the
    subset still runs. Test modules with no selected case are left out, and a
    module can be renamed — used to give each per-test PR its own file so the
    PRs stay independently mergeable.

    Returns ``(files, notes)``; notes explain anything that could not be sliced.
    """
    files: list[_PrFile] = []
    notes: list[str] = []
    sources: dict[str, str] = {}
    selected_modules = 0
    cases = source.cases

    for path in sorted(source.directory.rglob("*")):
        if not _should_include(path):
            continue
        rel = path.relative_to(source.directory).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("Skipping non-text file for PR: %s", path)
            continue

        if not _is_python_test_module(path):
            files.append(
                _PrFile(
                    path=_repo_path(base, source.ticket_id, rel),
                    content=content,
                    kind=_module_kind(path),
                )
            )
            continue

        sources[path.stem] = content
        try:
            sliced = slice_module(content, cases, header_note=header_note)
        except SyntaxError as exc:
            notes.append(
                f"{rel} could not be filtered ({exc.msg}) — included unchanged"
            )
            logger.warning("Slicing %s failed: %s", path, exc)
            files.append(
                _PrFile(
                    path=_repo_path(base, source.ticket_id, rel),
                    content=content,
                    kind="test",
                )
            )
            selected_modules += 1
            continue

        if sliced is None:
            continue

        filename = rename_module_to or rel
        files.append(
            _PrFile(
                path=_repo_path(base, source.ticket_id, filename),
                content=sliced,
                kind="test",
            )
        )
        selected_modules += 1

    for gone in missing_tests(cases, sources):
        notes.append(
            f"{gone.test_id or gone.func_name} is no longer in the generated files "
            "(renamed or regenerated) and was left out"
        )

    if not selected_modules:
        raise PullRequestError(
            "None of the selected tests were found in the current generated files — "
            "re-run the tests (or regenerate the suite) and try again"
        )

    return files, notes


def _parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.strip().strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise PullRequestError(
            "GITHUB_AUTOMATION_REPO must be 'owner/name' "
            f"(got {repo!r})"
        )
    return parts[0], parts[1]


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def resolve_token(settings: Settings) -> str:
    """GitHub token from settings, falling back to the authenticated `gh` CLI."""
    token = (settings.github_token or "").strip()
    if token:
        return token
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("gh auth token unavailable: %s", exc)
    raise CredentialError(
        "No GitHub credentials — set GITHUB_TOKEN in .env or authenticate the "
        "`gh` CLI (`gh auth login`)"
    )


_resolve_token = resolve_token  # backwards-compatible internal alias


def _api(
    method: str,
    url: str,
    token: str,
    *,
    json: Optional[dict] = None,
    timeout: int = 60,
) -> dict | list:
    resp = requests.request(
        method,
        url,
        headers=_headers(token),
        json=json,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        hint = credential_hint(resp.status_code, resp.text)
        if hint:
            raise CredentialError(hint)
        raise PullRequestError(f"GitHub API {resp.status_code}: {resp.text[:400]}")
    if not resp.content:
        return {}
    return resp.json()


def _upsert_file(
    owner: str,
    name: str,
    path: str,
    content: str,
    branch: str,
    token: str,
    message: str,
) -> None:
    api = f"https://api.github.com/repos/{owner}/{name}/contents/{path}"
    existing_sha: Optional[str] = None
    probe = requests.get(
        api,
        headers=_headers(token),
        params={"ref": branch},
        timeout=30,
    )
    if probe.status_code == 200:
        existing_sha = probe.json().get("sha")
    elif probe.status_code not in (404,):
        hint = credential_hint(probe.status_code, probe.text)
        if hint:
            raise CredentialError(hint)
        raise PullRequestError(f"GitHub contents probe failed: {probe.text[:300]}")

    payload: dict = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha
    _api("PUT", api, token, json=payload)


def _ensure_branch(
    owner: str,
    name: str,
    branch: str,
    base: str,
    token: str,
) -> None:
    repo_api = f"https://api.github.com/repos/{owner}/{name}"
    ref_api = f"{repo_api}/git/ref/heads/{branch}"
    exists = requests.get(ref_api, headers=_headers(token), timeout=30)
    if exists.status_code == 200:
        return
    if exists.status_code not in (404,):
        hint = credential_hint(exists.status_code, exists.text)
        if hint:
            raise CredentialError(hint)
        raise PullRequestError(f"Branch lookup failed: {exists.text[:300]}")

    base_ref = _api("GET", f"{repo_api}/git/ref/heads/{base}", token)
    if not isinstance(base_ref, dict):
        raise PullRequestError("Unexpected base ref response")
    sha = base_ref["object"]["sha"]
    _api(
        "POST",
        f"{repo_api}/git/refs",
        token,
        json={"ref": f"refs/heads/{branch}", "sha": sha},
    )


def _find_open_pr(
    owner: str,
    name: str,
    branch: str,
    base: str,
    token: str,
) -> Optional[str]:
    url = (
        f"https://api.github.com/repos/{owner}/{name}/pulls"
        f"?state=open&head={owner}:{branch}&base={base}"
    )
    pulls = _api("GET", url, token)
    if isinstance(pulls, list) and pulls:
        return pulls[0].get("html_url")
    return None


def _ticket_label(sources: Sequence[TicketSource]) -> str:
    """Ticket identifier(s) for titles and messages."""
    ids = [s.safe_id for s in sources]
    if len(ids) == 1:
        return ids[0]
    if len(ids) <= 3:
        return ", ".join(ids)
    return f"{', '.join(ids[:3])} +{len(ids) - 3} more"


def _scope_copy(
    scope: str,
    sources: Sequence[TicketSource],
) -> tuple[str, str, str, str]:
    """
    Branch suffix, PR title, scope label, and summary bullet for a PR scope.

    ``all`` ships the whole suite, ``passed`` only the tests that passed, and
    ``test`` a single case so each test can be reviewed and merged on its own.
    """
    tickets = _ticket_label(sources)
    combined = len(sources) > 1
    case_total = sum(len(s.cases) for s in sources)

    if scope == "passed":
        return (
            "-passed",
            f"Add passing automation for {tickets}",
            "passing tests only",
            f"- Contains only the {case_total} test(s) that passed in the last run.",
        )
    if scope == "test":
        case = sources[0].cases[0]
        return (
            f"-{case.slug()}",
            f"Add {case.func_name} for {tickets}",
            f"single test: {case.test_id or case.func_name}",
            (
                f"- Contains the single test `{case.func_name}` "
                f"(last run: **{case.outcome}**), isolated in its own file so it "
                "can be reviewed and merged independently."
            ),
        )
    if combined:
        return (
            "",
            f"Add automation for {tickets}",
            f"all tests across {len(sources)} tickets",
            (
                f"- Combines the full generated suites for {len(sources)} tickets "
                f"({tickets}) into a single pull request."
            ),
        )
    return (
        "",
        f"Add automation for {tickets}",
        "all tests",
        "- Contains the full generated suite for this ticket, passing or not.",
    )


def _default_branch_stem(sources: Sequence[TicketSource], prefix: str) -> str:
    stem = (prefix or "automation").strip("/")
    if len(sources) == 1:
        return f"{stem}/{sources[0].ticket_id}"
    return f"{stem}/{sources[0].safe_id}-and-{len(sources) - 1}-more"


def _resolve_branch(
    options: PrOptions,
    sources: Sequence[TicketSource],
    suffix: str,
    scope: str,
    prefix: str,
) -> str:
    """
    Final branch name: the user's if given, otherwise ``<prefix>/<ticket>``.

    A scope suffix is appended to auto-generated names so the scopes coexist. A
    user-supplied name is used verbatim — except for per-test PRs, where the
    test slug still has to be appended or every PR in the fan-out would target
    one branch and overwrite the last.
    """
    requested = sanitize_branch(options.branch or "")
    if requested:
        if scope == "test":
            return sanitize_branch(f"{requested}{suffix}")
        return requested

    auto = _default_branch_stem(sources, prefix)
    return sanitize_branch(f"{auto}{suffix}")


def _resolve_target_path(
    options: PrOptions,
    conventions: RepoConventions,
    settings: Settings,
) -> tuple[str, list[str]]:
    """
    Where generated files land in the automation repo, and why.

    Precedence: an explicit path from the UI, then the test root discovered in
    the repo, then the configured fallback. Returning the reason keeps the PR
    body honest about whether placement was chosen or merely defaulted.
    """
    notes: list[str] = []
    if options.target_path is not None and options.target_path.strip():
        path = sanitize_repo_dir(options.target_path)
        notes.append(f"Target path `{path or '(repo root)'}` was set from the dashboard")
        return path, notes

    # An empty test_root is a real answer when the repo keeps its tests at the
    # top level, so key off whether any test files were found, not the path.
    if conventions.analyzed and conventions.test_file_count:
        path = sanitize_repo_dir(conventions.test_root)
        notes.append(
            f"Placed under `{path}/` to match where {conventions.repo} already "
            "keeps its tests"
            if path
            else f"Placed at the repository root, where {conventions.repo} keeps "
            "its own test files"
        )
        return path, notes

    fallback = sanitize_repo_dir(settings.github_tests_path or "tests")
    if conventions.analyzed:
        notes.append(
            f"No existing test tree found in {conventions.repo} — used the "
            f"configured default `{fallback or '(repo root)'}`"
        )
    return fallback, notes


def _adapt_files(
    files: list[_PrFile],
    conventions: RepoConventions,
    settings: Settings,
) -> tuple[list[str], list[str]]:
    """
    Rewrite staged Python modules to the repo's conventions, in place.

    Returns ``(adapted_paths, notes)``. Rejected rewrites leave the file exactly
    as generated and add an explanatory note — adaptation can only improve the
    fit, never break the PR.
    """
    adapted: list[str] = []
    notes: list[str] = []
    modules = [f for f in files if f.kind]
    if not modules:
        return adapted, notes

    if len(modules) > MAX_ADAPTED_MODULES:
        notes.append(
            f"{len(modules)} modules staged but at most {MAX_ADAPTED_MODULES} are "
            "adapted per PR — the rest were pushed as generated"
        )
        modules = modules[:MAX_ADAPTED_MODULES]

    for staged in modules:
        result = adapt_module(
            staged.content,
            conventions=conventions,
            module_path=staged.path,
            kind=staged.kind,
            settings=settings,
        )
        staged.content = result.content
        if result.changed:
            adapted.append(staged.path)
        if result.note:
            notes.append(result.note)
    return adapted, notes


def create_automation_pr(
    sources: Sequence[TicketSource],
    *,
    scope: str = "all",
    options: Optional[PrOptions] = None,
    settings: Optional[Settings] = None,
) -> PullRequestResult:
    """
    Push generated automation files and open one PR for ``sources``.

    ``scope`` picks what the PR carries:

    - ``all`` — every generated file, whatever the test outcomes were.
    - ``passed`` — modules sliced down to each source's ``cases``.
    - ``test`` — a single case, moved into its own module file so per-test PRs
      do not conflict with each other.

    Grouping is the caller's decision: pass one source for a per-ticket PR or
    several to combine them into a single PR. ``options`` carries the
    user-controlled branch name, target directory, and repo-awareness toggles.
    Re-raising the same branch upserts the files and reuses the open PR.
    """
    cfg = settings or get_settings()
    opts = options or PrOptions()
    repo = (cfg.github_automation_repo or "").strip()
    if not repo:
        raise PullRequestError(
            "GITHUB_AUTOMATION_REPO is not configured (expected owner/name)"
        )

    selected = [s for s in sources if s.ticket_id]
    if not selected:
        raise PullRequestError("No tickets selected for the PR")
    if scope in {"passed", "test"} and not any(s.cases for s in selected):
        raise PullRequestError(f"PR scope {scope!r} needs at least one test case")
    if scope == "test" and (len(selected) != 1 or len(selected[0].cases) != 1):
        raise PullRequestError("PR scope 'test' takes exactly one test case")

    owner, name = _parse_repo(repo)
    token = resolve_token(cfg)
    base = (cfg.github_pr_base_branch or "main").strip() or "main"

    suffix, auto_title, label, scope_bullet = _scope_copy(scope, selected)
    title = (opts.title or "").strip() or auto_title
    branch = _resolve_branch(opts, selected, suffix, scope, cfg.github_pr_branch_prefix)
    if not branch:
        raise PullRequestError("Branch name is empty after sanitising — pick another")

    notes: list[str] = []

    conventions = RepoConventions(repo=repo, ref=base)
    if opts.analyze_repo:
        conventions = analyze_repo(
            owner, name, base, token, refresh=opts.refresh_analysis
        )
        if not conventions.analyzed:
            notes.extend(conventions.notes)

    target_path, placement_notes = _resolve_target_path(opts, conventions, cfg)
    notes.extend(placement_notes)

    files: list[_PrFile] = []
    for source in selected:
        if not source.directory.is_dir():
            raise PullRequestError(f"No generated output found at {source.directory}")
        if scope == "all":
            files.extend(_collect_files(source, target_path))
            continue
        header = f"Generated by QA Script Generator — {label} for {source.safe_id}"
        rename = (
            f"{source.cases[0].func_name}.py"
            if scope == "test" and source.cases
            else None
        )
        collected, source_notes = _collect_selected_files(
            source,
            target_path,
            header_note=header,
            rename_module_to=rename,
        )
        files.extend(collected)
        notes.extend(source_notes)

    if not files:
        raise PullRequestError(
            "No automation files found in "
            + ", ".join(str(s.directory) for s in selected)
        )

    adapted_files: list[str] = []
    if opts.adapt_tests:
        if not conventions.analyzed:
            notes.append(
                "Tests were not adapted — the target repo could not be analyzed"
            )
        else:
            adapted_files, adapt_notes = _adapt_files(files, conventions, cfg)
            notes.extend(adapt_notes)

    tickets = [s.ticket_id for s in selected]
    logger.info(
        "Creating %s automation PR for %s → %s@%s under %s/ (%d files, %d test(s))",
        scope,
        ", ".join(tickets),
        repo,
        branch,
        target_path or "(root)",
        len(files),
        sum(len(s.cases) for s in selected),
    )

    _ensure_branch(owner, name, branch, base, token)

    commit_label = _ticket_label(selected)
    for staged in files:
        _upsert_file(
            owner,
            name,
            staged.path,
            staged.content,
            branch,
            token,
            message=f"Add {staged.path} for {commit_label}",
        )

    test_names = [c.test_id or c.func_name for s in selected for c in s.cases]
    convention_lines = conventions.summary_lines() if opts.analyze_repo else []

    existing = _find_open_pr(owner, name, branch, base, token)
    if existing:
        return PullRequestResult(
            url=existing,
            branch=branch,
            repo=repo,
            created=False,
            message=f"Updated existing PR for {commit_label} ({label})",
            scope=scope,
            label=label,
            file_count=len(files),
            tests=test_names,
            notes=notes,
            tickets=tickets,
            target_path=target_path,
            title=title,
            conventions=convention_lines,
            adapted_files=adapted_files,
        )

    body_lines = [
        "## Summary",
        f"- Generated API automation for Jira ticket(s) **{commit_label}**.",
        scope_bullet,
        f"- Files land under `{target_path or '(repo root)'}/`.",
        f"- Raised {opts.trigger}; a green suite is not required.",
        "",
        "## Test results",
        f"- {opts.test_summary or 'No test run recorded — see CI / local pytest output for details.'}",
    ]
    if convention_lines:
        body_lines += ["", "## Target repo conventions detected"]
        body_lines += [f"- {line}" for line in convention_lines]
    if adapted_files:
        body_lines += [
            "",
            "## Adapted to this repo",
            "- The modules below were rewritten to reuse the repo's existing "
            "fixtures, helpers, and style. Review the fixture wiring before merging.",
        ]
        body_lines += [f"  - `{path}`" for path in adapted_files]
    if scope != "all" and test_names:
        body_lines += ["", "## Tests included"]
        body_lines += [f"- `{tid}`" for tid in test_names[:50]]
        if len(test_names) > 50:
            body_lines.append(f"- …and {len(test_names) - 50} more")
    if notes:
        body_lines += ["", "## Notes"]
        body_lines += [f"- {note}" for note in notes]
    body_lines += [
        "",
        "## Test plan",
        "- [ ] Reviewers: confirm coverage against ticket acceptance criteria",
        "- [ ] Investigate any failing cases listed in the run summary",
    ]

    pr = _api(
        "POST",
        f"https://api.github.com/repos/{owner}/{name}/pulls",
        token,
        json={
            "title": title,
            "head": branch,
            "base": base,
            "body": "\n".join(body_lines),
        },
    )
    if not isinstance(pr, dict) or not pr.get("html_url"):
        raise PullRequestError("GitHub PR create returned no URL")

    return PullRequestResult(
        url=pr["html_url"],
        branch=branch,
        repo=repo,
        created=True,
        message=f"Opened PR for {commit_label} ({label})",
        scope=scope,
        label=label,
        file_count=len(files),
        tests=test_names,
        notes=notes,
        tickets=tickets,
        target_path=target_path,
        title=title,
        conventions=convention_lines,
        adapted_files=adapted_files,
    )


def describe_target_repo(
    *,
    settings: Optional[Settings] = None,
    refresh: bool = False,
) -> RepoConventions:
    """
    Analyze the configured automation repo so the dashboard can show and
    pre-fill what was found before anyone raises a PR.
    """
    cfg = settings or get_settings()
    repo = (cfg.github_automation_repo or "").strip()
    if not repo:
        raise PullRequestError(
            "GITHUB_AUTOMATION_REPO is not configured (expected owner/name)"
        )
    owner, name = _parse_repo(repo)
    base = (cfg.github_pr_base_branch or "main").strip() or "main"
    return analyze_repo(owner, name, base, resolve_token(cfg), refresh=refresh)
