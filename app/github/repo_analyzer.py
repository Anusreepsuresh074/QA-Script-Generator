"""
Read the target automation repository and work out how it writes tests.

Before an automation PR is raised, the generated suite should land where the
repo already keeps its tests, named the way that repo names things, reusing the
fixtures it already has. This module answers those questions from the GitHub
API alone (no clone): it walks the default-branch tree, picks out the test root
and naming pattern, parses ``conftest.py`` for available fixtures, reads the
pytest config for markers and ``testpaths``, and samples a representative test
module for style.

The result is advisory. Analysis never blocks a PR — when the repo cannot be
read, :class:`RepoConventions` comes back with ``analyzed=False`` and the caller
falls back to its configured defaults.
"""

from __future__ import annotations

import ast
import base64
import configparser
import fnmatch
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# Analysis is a nicety on the PR path, so keep the API budget small and fixed.
_MAX_BLOB_FETCHES = 14
_MAX_BLOB_BYTES = 120_000
_MAX_TREE_ENTRIES = 20_000
_SAMPLE_MODULE_LINES = 120
_CACHE_TTL_SECONDS = 300

_TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
_CONFIG_FILES = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
_HELPER_HINTS = (
    "helper",
    "util",
    "client",
    "common",
    "support",
    "factory",
    "factories",
    "fixture",
    "payload",
    "schema",
    "constant",
    "config",
    "base",
)
# Directories that are never a sensible home for generated tests.
_IGNORED_DIR_PARTS = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "site-packages",
    "build",
    "dist",
    ".tox",
    ".pytest_cache",
}

_cache: dict[tuple[str, str], tuple[float, "RepoConventions"]] = {}


def credential_hint(status: int, body: str) -> Optional[str]:
    """
    A credential-specific explanation for a GitHub failure, if that is the cause.

    Expired tokens are the most common reason automation PRs stop working, and
    a bare "GitHub API 401" tells nobody what to do about it — so name the
    cause and the fix. Shared with :mod:`app.github.pr_service`.
    """
    text = (body or "").lower()
    if status == 401:
        return (
            "GitHub rejected the credential (401) — GITHUB_TOKEN has expired or "
            "been revoked. Issue a new token (repo scope) and update .env, or "
            "re-run `gh auth login`"
        )
    if status == 403:
        if "rate limit" in text:
            return (
                "GitHub rate limit reached (403) — wait for the limit to reset "
                "or use a token with a higher quota"
            )
        if "saml" in text or "sso" in text:
            return (
                "GitHub blocked the credential (403) — the token needs SAML/SSO "
                "authorization for this organization"
            )
        return (
            "GitHub refused the request (403) — the token is missing the scope "
            "or repository access required (needs `repo` / read-write Contents "
            "and Pull requests)"
        )
    if status == 404 and "not found" in text:
        return (
            "GitHub returned 404 — either GITHUB_AUTOMATION_REPO is wrong or the "
            "token cannot see that repository (an expired or under-scoped token "
            "looks identical to a missing repo)"
        )
    return None


@dataclass
class RepoFixture:
    """A pytest fixture the target repo already provides."""

    name: str
    file: str
    scope: str = "function"
    params: list[str] = field(default_factory=list)
    doc: str = ""

    def signature(self) -> str:
        args = ", ".join(self.params)
        scope = f" (scope={self.scope})" if self.scope != "function" else ""
        return f"{self.name}({args}){scope}"


@dataclass
class RepoConventions:
    """
    What the target repo's own test suite looks like.

    ``analyzed`` is False when the repo could not be read; every field then
    holds its empty default and ``notes`` explains why.
    """

    repo: str = ""
    ref: str = ""
    analyzed: bool = False
    test_root: str = ""
    test_roots: list[str] = field(default_factory=list)
    naming_pattern: str = ""
    module_examples: list[str] = field(default_factory=list)
    conftest_paths: list[str] = field(default_factory=list)
    fixtures: list[RepoFixture] = field(default_factory=list)
    helpers: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    testpaths: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    style: dict[str, str] = field(default_factory=dict)
    uses_test_classes: bool = False
    sample_module_path: str = ""
    sample_module: str = ""
    test_file_count: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def fixture_names(self) -> list[str]:
        return [f.name for f in self.fixtures]

    def summary_lines(self) -> list[str]:
        """Human-readable findings, for the PR body and the dashboard."""
        if not self.analyzed:
            return [f"Repo analysis unavailable: {'; '.join(self.notes) or 'unknown reason'}"]

        lines = [
            f"Test root: `{self.test_root or '(repo root)'}` "
            f"({self.test_file_count} test file(s) found)"
        ]
        if self.naming_pattern:
            lines.append(f"Naming: `{self.naming_pattern}`")
        if self.uses_test_classes:
            lines.append("Existing tests group cases in `Test*` classes")
        if self.conftest_paths:
            lines.append("conftest: " + ", ".join(f"`{p}`" for p in self.conftest_paths[:4]))
        if self.fixtures:
            shown = ", ".join(f"`{f.signature()}`" for f in self.fixtures[:10])
            more = f" (+{len(self.fixtures) - 10} more)" if len(self.fixtures) > 10 else ""
            lines.append(f"Fixtures available: {shown}{more}")
        if self.helpers:
            lines.append("Helpers: " + ", ".join(f"`{h}`" for h in self.helpers[:6]))
        if self.config_files:
            lines.append("Pytest config: " + ", ".join(f"`{c}`" for c in self.config_files))
        if self.markers:
            lines.append("Registered markers: " + ", ".join(f"`{m}`" for m in self.markers[:12]))
        if self.testpaths:
            lines.append("testpaths: " + ", ".join(f"`{p}`" for p in self.testpaths))
        if self.style:
            lines.append(
                "Style: "
                + ", ".join(f"{k.replace('_', ' ')}={v}" for k, v in sorted(self.style.items()))
            )
        if self.requirements:
            lines.append("Test deps: " + ", ".join(f"`{r}`" for r in self.requirements[:10]))
        lines.extend(self.notes)
        return lines

    def prompt_context(self) -> str:
        """Compact description of the repo for the test-adaptation LLM call."""
        if not self.analyzed:
            return ""

        parts = [f"Repository: {self.repo} (branch {self.ref})"]
        parts.append(f"Tests live in: {self.test_root or 'the repository root'}")
        if self.naming_pattern:
            parts.append(f"Test module naming: {self.naming_pattern}")
        parts.append(
            "Test cases are grouped in Test* classes"
            if self.uses_test_classes
            else "Test cases are plain module-level test_* functions"
        )
        if self.fixtures:
            parts.append("Fixtures already provided by the repo's conftest.py:")
            for fx in self.fixtures[:20]:
                doc = f" — {fx.doc}" if fx.doc else ""
                parts.append(f"  - {fx.signature()} in {fx.file}{doc}")
        if self.helpers:
            parts.append("Shared helper modules: " + ", ".join(self.helpers[:10]))
        if self.markers:
            parts.append("Markers registered in pytest config: " + ", ".join(self.markers[:15]))
        if self.requirements:
            parts.append("Libraries available: " + ", ".join(self.requirements[:15]))
        if self.style:
            parts.append(
                "Code style: "
                + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(self.style.items()))
            )
        if self.sample_module:
            parts.append(
                f"\nRepresentative existing test module ({self.sample_module_path}):\n"
                f"```python\n{self.sample_module}\n```"
            )
        return "\n".join(parts)


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _is_ignored(path: str) -> bool:
    return any(part in _IGNORED_DIR_PARTS for part in path.split("/"))


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in _TEST_FILE_PATTERNS)


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _fetch_tree(owner: str, name: str, ref: str, token: str) -> list[dict[str, Any]]:
    url = (
        f"https://api.github.com/repos/{owner}/{name}/git/trees/"
        f"{requests.utils.quote(ref, safe='')}?recursive=1"
    )
    resp = requests.get(url, headers=_headers(token), timeout=45)
    if resp.status_code >= 400:
        hint = credential_hint(resp.status_code, resp.text)
        raise RuntimeError(
            hint or f"tree fetch failed ({resp.status_code}): {resp.text[:200]}"
        )
    payload = resp.json()
    entries = payload.get("tree") or []
    if payload.get("truncated"):
        logger.info("Tree for %s/%s truncated — analysis uses the first page", owner, name)
    return entries[:_MAX_TREE_ENTRIES]


def _fetch_text(owner: str, name: str, path: str, ref: str, token: str) -> Optional[str]:
    url = f"https://api.github.com/repos/{owner}/{name}/contents/{requests.utils.quote(path)}"
    resp = requests.get(url, headers=_headers(token), params={"ref": ref}, timeout=30)
    if resp.status_code >= 400:
        logger.debug("Could not read %s: HTTP %s", path, resp.status_code)
        return None
    payload = resp.json()
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    try:
        raw = base64.b64decode(payload.get("content") or "")
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        logger.debug("Skipping non-UTF-8 file %s", path)
        return None


def _infer_test_root(test_files: list[str]) -> tuple[str, list[str]]:
    """
    The directory the repo keeps tests in, plus the candidates considered.

    Test files usually sit under one tree (``tests/``, ``qa/tests/api/``), often
    split across subfolders. The shared prefix of their directories is the root;
    when files are spread across unrelated trees the busiest directory wins so
    generated tests join the largest existing group rather than a stray one.
    """
    dirs = [_parent(p) for p in test_files]
    counts = Counter(dirs)
    candidates = [d for d, _ in counts.most_common(8)]

    non_root = [d for d in dirs if d]
    if not non_root:
        return "", candidates

    split = [d.split("/") for d in non_root]
    prefix: list[str] = []
    for segments in zip(*split):
        if len(set(segments)) != 1:
            break
        prefix.append(segments[0])

    if prefix:
        return "/".join(prefix), candidates

    busiest, _ = counts.most_common(1)[0]
    return busiest, candidates


def _infer_naming(test_files: list[str]) -> str:
    names = [p.rsplit("/", 1)[-1] for p in test_files]
    prefixed = sum(1 for n in names if n.startswith("test_"))
    suffixed = sum(1 for n in names if n.endswith("_test.py"))
    if suffixed > prefixed:
        return "<name>_test.py"
    if prefixed:
        return "test_<name>.py"
    return ""


def _fixtures_from_source(source: str, path: str) -> list[RepoFixture]:
    """Fixtures declared in a conftest/module, read straight off the AST."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        logger.debug("Could not parse %s: %s", path, exc)
        return []

    found: list[RepoFixture] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            target = call.func if call else decorator
            label = ""
            if isinstance(target, ast.Attribute):
                label = target.attr
            elif isinstance(target, ast.Name):
                label = target.id
            if label != "fixture":
                continue

            scope = "function"
            if call:
                for kw in call.keywords:
                    if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                        scope = str(kw.value.value)
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            found.append(
                RepoFixture(
                    name=node.name,
                    file=path,
                    scope=scope,
                    params=[a.arg for a in node.args.args],
                    doc=doc[0] if doc else "",
                )
            )
            break
    return found


def _marker_name(entry: str) -> str:
    """``smoke: quick checks`` / ``copied_from(src, x=1): …`` -> ``smoke`` / ``copied_from``."""
    return entry.split(":", 1)[0].split("(", 1)[0].strip()


def _markers_from_ini(text: str, section: str) -> tuple[list[str], list[str]]:
    """``markers`` and ``testpaths`` out of an ini-style pytest section."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        logger.debug("Could not parse ini config: %s", exc)
        return [], []
    if not parser.has_section(section):
        return [], []

    markers: list[str] = []
    for line in (parser.get(section, "markers", fallback="") or "").splitlines():
        marker = _marker_name(line)
        if marker:
            markers.append(marker)
    testpaths = (parser.get(section, "testpaths", fallback="") or "").split()
    return markers, testpaths


def _markers_from_pyproject(text: str) -> tuple[list[str], list[str]]:
    """
    ``markers`` / ``testpaths`` from ``[tool.pytest.ini_options]``.

    Parsed with a narrow regex rather than a TOML library so analysis works on
    Python builds without ``tomllib`` and never raises on an odd file.
    """
    block = re.search(
        r"\[tool\.pytest\.ini_options\](.*?)(?=\n\[|\Z)", text, re.DOTALL
    )
    if not block:
        return [], []
    body = block.group(1)

    def _list_value(key: str) -> list[str]:
        match = re.search(rf"^\s*{key}\s*=\s*(\[.*?\]|\".*?\"|'.*?')", body, re.DOTALL | re.MULTILINE)
        if not match:
            return []
        return [v for v in re.findall(r"[\"']([^\"']+)[\"']", match.group(1)) if v.strip()]

    markers = [_marker_name(m) for m in _list_value("markers")]
    return [m for m in markers if m], _list_value("testpaths")


def _requirements_from_text(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    return names


def _dependencies_from_pyproject(text: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL):
        for dep in re.findall(r"[\"']([^\"']+)[\"']", match.group(1)):
            name = re.split(r"[<>=!~\[; ]", dep, maxsplit=1)[0].strip()
            if name:
                names.append(name)
    return names


def _style_from_source(source: str) -> dict[str, str]:
    """Coarse style read of a representative module: indent, quotes, hints."""
    style: dict[str, str] = {}
    lines = source.splitlines()

    indents = [
        len(line) - len(line.lstrip(" "))
        for line in lines
        if line.startswith(" ") and line.strip()
    ]
    if indents:
        style["indent"] = f"{min(indents)} spaces"

    double = len(re.findall(r'"[^"\n]*"', source))
    single = len(re.findall(r"'[^'\n]*'", source))
    if double or single:
        style["quotes"] = "double" if double >= single else "single"

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return style

    funcs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if funcs:
        annotated = sum(1 for f in funcs if f.returns is not None)
        style["type hints"] = "yes" if annotated > len(funcs) / 2 else "no"
        documented = sum(1 for f in funcs if ast.get_docstring(f))
        style["docstrings"] = "yes" if documented > len(funcs) / 2 else "sparse"
    if any(isinstance(n, ast.AsyncFunctionDef) for n in funcs):
        style["async tests"] = "yes"
    return style


def _pick_sample_module(test_files: list[str], test_root: str) -> Optional[str]:
    """A test module under the detected root, preferring a small top-level one."""
    scoped = [p for p in test_files if not test_root or p.startswith(f"{test_root}/")]
    pool = scoped or test_files
    if not pool:
        return None
    return sorted(pool, key=lambda p: (p.count("/"), len(p)))[0]


def _collect_helpers(entries: list[dict[str, Any]], test_root: str) -> list[str]:
    """
    Non-test Python modules the repo's tests can import.

    Deliberately restricted to the test tree. Matching helper-ish names across
    the whole repo drags in library sources (``src/pkg/utils.py``,
    ``httpx/_client.py``), and offering those to the adaptation step invites
    generated tests to import package internals they have no business touching.
    """
    helpers: list[str] = []
    for entry in entries:
        path = entry.get("path") or ""
        if entry.get("type") != "blob" or not path.endswith(".py"):
            continue
        if _is_ignored(path) or _is_test_file(path):
            continue
        name = path.rsplit("/", 1)[-1]
        if name in {"conftest.py", "__init__.py", "setup.py"}:
            continue
        if test_root:
            if path.startswith(f"{test_root}/"):
                helpers.append(path)
            continue
        # Tests sit at the repository root: only take root-level helper modules.
        if "/" not in path and any(hint in name.lower() for hint in _HELPER_HINTS):
            helpers.append(path)
    return sorted(helpers, key=lambda p: (p.count("/"), p))[:12]


def analyze_repo(
    owner: str,
    name: str,
    ref: str,
    token: str,
    *,
    refresh: bool = False,
) -> RepoConventions:
    """
    Inspect ``owner/name`` at ``ref`` and describe how it writes tests.

    Results are cached briefly per repo+ref: a PR run makes several calls and
    the repo does not change underneath them. Any failure is reported through
    ``analyzed=False`` plus ``notes`` — this never raises.
    """
    repo = f"{owner}/{name}"
    key = (repo, ref)
    cached = _cache.get(key)
    if cached and not refresh and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    conventions = RepoConventions(repo=repo, ref=ref)
    try:
        entries = _fetch_tree(owner, name, ref, token)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        conventions.notes.append(f"could not read {repo}@{ref} ({exc})")
        logger.warning("Repo analysis failed for %s@%s: %s", repo, ref, exc)
        return conventions

    blobs = [
        e for e in entries
        if e.get("type") == "blob" and not _is_ignored(e.get("path") or "")
    ]
    paths = [e.get("path") or "" for e in blobs]
    test_files = [p for p in paths if _is_test_file(p)]
    conftests = sorted(
        (p for p in paths if p.rsplit("/", 1)[-1] == "conftest.py"),
        key=lambda p: (p.count("/"), p),
    )
    config_files = [p for p in paths if p in _CONFIG_FILES or p.rsplit("/", 1)[-1] in _CONFIG_FILES]

    conventions.analyzed = True
    conventions.test_file_count = len(test_files)
    conventions.conftest_paths = conftests[:6]
    conventions.config_files = sorted(set(config_files), key=lambda p: (p.count("/"), p))[:6]

    if test_files:
        conventions.test_root, conventions.test_roots = _infer_test_root(test_files)
        conventions.naming_pattern = _infer_naming(test_files)
        conventions.module_examples = [
            p.rsplit("/", 1)[-1] for p in sorted(test_files, key=len)[:6]
        ]
    else:
        conventions.notes.append(
            "no existing test files found — generated tests will use the "
            "configured default path"
        )

    conventions.helpers = _collect_helpers(blobs, conventions.test_root)

    budget = _MAX_BLOB_FETCHES
    sizes = {e.get("path"): int(e.get("size") or 0) for e in blobs}

    def _read(path: str) -> Optional[str]:
        nonlocal budget
        if budget <= 0 or sizes.get(path, 0) > _MAX_BLOB_BYTES:
            return None
        budget -= 1
        return _fetch_text(owner, name, path, ref, token)

    # conftest.py — the fixtures generated tests should be reusing
    for path in conventions.conftest_paths[:4]:
        source = _read(path)
        if source:
            conventions.fixtures.extend(_fixtures_from_source(source, path))

    # pytest config — markers and testpaths the suite is expected to declare
    for path in conventions.config_files:
        text = _read(path)
        if not text:
            continue
        base = path.rsplit("/", 1)[-1]
        if base == "pytest.ini":
            markers, testpaths = _markers_from_ini(text, "pytest")
        elif base == "tox.ini":
            markers, testpaths = _markers_from_ini(text, "pytest")
        elif base == "setup.cfg":
            markers, testpaths = _markers_from_ini(text, "tool:pytest")
        elif base == "pyproject.toml":
            markers, testpaths = _markers_from_pyproject(text)
            conventions.requirements.extend(_dependencies_from_pyproject(text))
        else:
            markers, testpaths = [], []
        conventions.markers.extend(markers)
        conventions.testpaths.extend(testpaths)

    # declared test dependencies, so adaptation only reaches for what is there
    for path in sorted(
        (p for p in paths if p.rsplit("/", 1)[-1].startswith("requirements")),
        key=lambda p: (p.count("/"), p),
    )[:2]:
        text = _read(path)
        if text:
            conventions.requirements.extend(_requirements_from_text(text))

    sample_path = _pick_sample_module(test_files, conventions.test_root)
    if sample_path:
        source = _read(sample_path)
        if source:
            conventions.sample_module_path = sample_path
            lines = source.splitlines()
            conventions.sample_module = "\n".join(lines[:_SAMPLE_MODULE_LINES])
            if len(lines) > _SAMPLE_MODULE_LINES:
                conventions.sample_module += "\n# … truncated"
            conventions.style = _style_from_source(source)
            conventions.uses_test_classes = bool(
                re.search(r"^class\s+Test\w+", source, re.MULTILINE)
            )
            conventions.fixtures.extend(
                fx for fx in _fixtures_from_source(source, sample_path)
                if fx.name not in conventions.fixture_names
            )

    # de-duplicate while keeping first-seen order
    conventions.markers = list(dict.fromkeys(conventions.markers))
    conventions.testpaths = list(dict.fromkeys(conventions.testpaths))
    conventions.requirements = list(dict.fromkeys(conventions.requirements))
    seen: set[str] = set()
    unique_fixtures: list[RepoFixture] = []
    for fixture in conventions.fixtures:
        if fixture.name in seen:
            continue
        seen.add(fixture.name)
        unique_fixtures.append(fixture)
    conventions.fixtures = unique_fixtures

    logger.info(
        "Analyzed %s@%s: test_root=%r, %d test file(s), %d fixture(s)",
        repo,
        ref,
        conventions.test_root,
        conventions.test_file_count,
        len(conventions.fixtures),
    )
    _cache[key] = (time.monotonic(), conventions)
    return conventions


def clear_cache() -> None:
    """Drop cached analyses (used by the dashboard's refresh action)."""
    _cache.clear()
