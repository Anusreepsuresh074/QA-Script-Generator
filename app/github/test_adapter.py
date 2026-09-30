"""
Rewrite generated Pytest modules to fit the target repository's conventions.

:mod:`app.github.repo_analyzer` works out how the automation repo writes tests;
this module hands that description to Groq along with a generated module and
asks for the same coverage expressed the repo's way — its fixtures instead of
a freshly built session, its helpers instead of inlined constants, its markers,
its style.

Adaptation is strictly opt-in and never trusted blindly. A rewrite is accepted
only if it parses and still contains every test (or fixture) the original had;
otherwise the original file is pushed unchanged and a note explains why. The
worst case is therefore the previous behaviour, not a broken PR.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.ai.groq_client import GroqClient, GroqTransientError
from app.config.settings import Settings
from app.github.repo_analyzer import RepoConventions

logger = logging.getLogger(__name__)

# One Groq call per module, so cap how many a single PR can trigger.
MAX_ADAPTED_MODULES = 6
_MAX_SOURCE_CHARS = 24_000

_SYSTEM_PROMPT = """You are a senior QA automation engineer landing generated \
API tests into an existing repository.

Rewrite the given Pytest module so it looks like it was written by that \
repository's own maintainers, while testing exactly the same things.

Rules:
- Preserve every test function, its name, and what it asserts. Never drop, \
merge, rename, or weaken a test, and never add new ones.
- Reuse the repository's existing conftest fixtures and helper modules instead \
of re-creating sessions, base URLs, auth headers, or constants locally. Request \
a fixture by adding it as a test parameter.
- Only import libraries and helpers the repository actually has.
- Match the repository's naming, indentation, quoting, docstring, type-hint and \
class-vs-function conventions.
- Keep pytest markers that the repository registers; drop markers it does not.
- Do not invent fixtures, helpers, endpoints, or credentials that were not \
listed or present in the original module.

Return ONLY the complete rewritten Python module. No prose, no explanation, no \
markdown fences."""


@dataclass
class AdaptationResult:
    """Outcome of one module rewrite."""

    content: str
    changed: bool = False
    note: str = ""
    notes: list[str] = field(default_factory=list)


def _strip_fences(text: str) -> str:
    """Drop ```python fences the model adds despite being asked not to."""
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    cleaned = re.sub(r"^```[a-zA-Z0-9_+-]*\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


def _test_names(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }


def _fixture_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            attr = getattr(target, "attr", None) or getattr(target, "id", None)
            if attr == "fixture":
                names.add(node.name)
                break
    return names


def _assert_count(tree: ast.AST) -> int:
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))


def _validate(original: str, candidate: str, *, kind: str) -> Optional[str]:
    """
    Reject a rewrite that lost coverage. Returns a reason, or None if it is safe.

    Cheap structural checks only — enough to catch the failure modes that
    matter: a module that no longer parses, tests quietly dropped or renamed,
    or assertions stripped out to make a test pass.
    """
    try:
        before = ast.parse(original)
    except SyntaxError:
        return "the generated module itself does not parse"

    try:
        after = ast.parse(candidate)
    except SyntaxError as exc:
        return f"the rewrite does not parse ({exc.msg} on line {exc.lineno})"

    if kind == "conftest":
        lost = _fixture_names(before) - _fixture_names(after)
        if lost:
            return f"the rewrite dropped fixture(s): {', '.join(sorted(lost))}"
        return None

    lost_tests = _test_names(before) - _test_names(after)
    if lost_tests:
        return f"the rewrite dropped test(s): {', '.join(sorted(lost_tests))}"

    before_asserts = _assert_count(before)
    if before_asserts and not _assert_count(after):
        return "the rewrite removed every assertion"
    if before_asserts and _assert_count(after) < before_asserts / 2:
        return "the rewrite removed more than half of the assertions"
    return None


def adapt_module(
    source: str,
    *,
    conventions: RepoConventions,
    module_path: str,
    kind: str = "test",
    settings: Optional[Settings] = None,
    client: Optional[GroqClient] = None,
) -> AdaptationResult:
    """
    Rewrite ``source`` to match ``conventions``, falling back to it unchanged.

    ``kind`` is ``"test"`` for a ``test_*.py`` module or ``"conftest"`` for a
    generated ``conftest.py`` — a conftest is validated on its fixtures rather
    than its tests, and is told to defer to fixtures the repo already defines.
    """
    context = conventions.prompt_context()
    if not context:
        return AdaptationResult(content=source, note="")

    if len(source) > _MAX_SOURCE_CHARS:
        note = f"{module_path} is too large to adapt ({len(source)} chars) — pushed unchanged"
        logger.info(note)
        return AdaptationResult(content=source, note=note)

    if kind == "conftest":
        instruction = (
            "This is a generated conftest.py. Remove fixtures the repository "
            "already provides (they are inherited from its own conftest.py) and "
            "keep only what this ticket's tests genuinely add. If nothing is "
            "left to add, return a minimal module that imports nothing and "
            "defines nothing beyond a short comment."
        )
    else:
        instruction = (
            "This is a generated test module. Rewrite it against the "
            "repository's conventions listed above."
        )

    user_prompt = (
        f"{context}\n\n"
        f"---\n{instruction}\n\n"
        f"Module path in the repository: {module_path}\n\n"
        f"```python\n{source}\n```"
    )

    groq = client or GroqClient(settings)
    try:
        raw = groq.chat_completion(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            temperature=0.1,
            max_tokens=8192,
        )
    except Exception as exc:  # noqa: BLE001 - adaptation must never block the PR
        note = f"{module_path} could not be adapted ({exc}) — pushed unchanged"
        # An expired GROQ_API_KEY is otherwise invisible here: the PR still
        # succeeds, just without adaptation. Say so plainly.
        if "api key is invalid" in str(exc).lower():
            note = (
                f"{module_path} was not adapted — GROQ_API_KEY is invalid or has "
                "expired; replace it in .env to re-enable repo-aware rewriting"
            )
        elif isinstance(exc, GroqTransientError):
            note = (
                f"{module_path} was not adapted — Groq was rate limited or "
                f"unavailable ({exc}); pushed unchanged"
            )
        logger.warning("Adaptation call failed for %s: %s", module_path, exc)
        return AdaptationResult(content=source, note=note)

    candidate = _strip_fences(raw or "")
    if not candidate:
        note = f"{module_path} adaptation returned nothing — pushed unchanged"
        logger.warning(note)
        return AdaptationResult(content=source, note=note)

    reason = _validate(source, candidate, kind=kind)
    if reason:
        note = f"{module_path} kept as generated — {reason}"
        logger.warning("Rejected adaptation of %s: %s", module_path, reason)
        return AdaptationResult(content=source, note=note)

    if candidate.strip() == source.strip():
        return AdaptationResult(content=source, note="")

    header = (
        f"# Adapted to {conventions.repo} conventions by QA Script Generator "
        f"— review fixture reuse before merging.\n"
    )
    logger.info("Adapted %s to %s conventions", module_path, conventions.repo)
    return AdaptationResult(
        content=header + candidate.rstrip() + "\n",
        changed=True,
        note=f"{module_path} was rewritten to reuse the repo's fixtures and style",
    )
