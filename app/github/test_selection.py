"""
Slice generated Pytest modules down to a chosen subset of test cases.

Used by the automation-PR flow so a PR can carry the whole suite, only the
tests that passed, or a single test per PR. Imports, fixtures, and helper
definitions are always preserved — only unselected ``test_*`` functions (and
``Test*`` classes left with no selected methods) are dropped.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

_PASSING_OUTCOMES = {"passed"}


class TestSelectionError(Exception):
    """Raised when a requested test cannot be located in the generated suite."""


@dataclass(frozen=True)
class TestCaseRef:
    """
    One test case as reported by pytest's JUnit XML.

    ``name`` is the test function (possibly with a ``[param]`` suffix) and
    ``classname`` the dotted path of module plus optional test class.
    """

    name: str
    classname: str = ""
    outcome: str = "passed"

    @property
    def func_name(self) -> str:
        """Function name with any parametrisation suffix stripped."""
        return self.name.split("[", 1)[0].strip()

    @property
    def module(self) -> str:
        """Module (file stem) the test lives in."""
        parts = [p for p in self.classname.split(".") if p]
        return parts[0] if parts else ""

    @property
    def class_name(self) -> Optional[str]:
        """Enclosing test class, when the case is a method."""
        parts = [p for p in self.classname.split(".") if p]
        return ".".join(parts[1:]) if len(parts) > 1 else None

    @property
    def test_id(self) -> str:
        """Stable identifier: ``module::func`` or ``module::Class::func``."""
        segments = [s for s in (self.module, self.class_name, self.func_name) if s]
        return "::".join(segments)

    @property
    def passed(self) -> bool:
        return self.outcome.lower() in _PASSING_OUTCOMES

    def slug(self) -> str:
        """Branch/file-safe form of the test identifier."""
        raw = "-".join(s for s in (self.class_name, self.func_name) if s)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
        return slug[:80] or "test"


def parse_case(case: dict) -> TestCaseRef:
    """Build a :class:`TestCaseRef` from a run-result dict."""
    return TestCaseRef(
        name=str(case.get("name") or "").strip(),
        classname=str(case.get("classname") or "").strip(),
        outcome=str(case.get("outcome") or "passed").strip(),
    )


def parse_cases(cases: Iterable[dict]) -> list[TestCaseRef]:
    return [ref for ref in (parse_case(c) for c in cases) if ref.func_name]


def resolve_test_ids(
    cases: Sequence[TestCaseRef],
    wanted: Sequence[str],
) -> list[TestCaseRef]:
    """
    Pick the cases matching ``wanted`` ids, accepting either the full
    ``module::func`` id or a bare function name.
    """
    by_id = {c.test_id: c for c in cases}
    by_func: dict[str, list[TestCaseRef]] = {}
    for case in cases:
        by_func.setdefault(case.func_name, []).append(case)

    resolved: list[TestCaseRef] = []
    for key in wanted:
        target = (key or "").strip()
        if not target:
            continue
        if target in by_id:
            resolved.append(by_id[target])
            continue
        matches = by_func.get(target.split("::")[-1], [])
        if len(matches) == 1:
            resolved.append(matches[0])
        elif not matches:
            raise TestSelectionError(f"Test {target!r} is not in the recorded run")
        else:
            raise TestSelectionError(
                f"Test {target!r} is ambiguous — use the module::test form"
            )
    return resolved


def _is_test_func(node: ast.AST) -> bool:
    return (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    )


def _is_test_class(node: ast.AST) -> bool:
    return isinstance(node, ast.ClassDef) and node.name.startswith("Test")


def _block_start(node: ast.AST) -> int:
    """1-based first source line of a node, decorators included."""
    decorators = getattr(node, "decorator_list", []) or []
    lines = [node.lineno] + [d.lineno for d in decorators]
    return min(lines)


def _source_block(lines: Sequence[str], node: ast.AST) -> list[str]:
    start = _block_start(node) - 1
    end = node.end_lineno  # inclusive 1-based -> exclusive 0-based
    return list(lines[start:end])


def _slice_class(
    lines: Sequence[str],
    node: ast.ClassDef,
    keep_methods: set[str],
) -> Optional[list[str]]:
    """Rebuild a test class containing only the selected test methods."""
    members = list(node.body)
    kept: list[ast.AST] = []
    has_selected_test = False

    for member in members:
        if _is_test_func(member):
            if member.name in keep_methods:
                kept.append(member)
                has_selected_test = True
        else:
            kept.append(member)

    if not has_selected_test:
        return None

    first_body_start = _block_start(members[0]) - 1
    header = list(lines[_block_start(node) - 1 : first_body_start])

    body: list[str] = []
    for member in kept:
        if body:
            body.append("")
        body.extend(_source_block(lines, member))

    return header + body


def slice_module(
    source: str,
    keep: Sequence[TestCaseRef],
    *,
    header_note: str = "",
) -> Optional[str]:
    """
    Return ``source`` reduced to the tests in ``keep``.

    ``None`` means no selected test lives in this module, so the file should be
    left out of the PR entirely. Raises :class:`SyntaxError` if the module (or
    the sliced result) does not parse.
    """
    tree = ast.parse(source)
    lines = source.splitlines()

    keep_funcs = {c.func_name for c in keep if not c.class_name}
    keep_by_class: dict[str, set[str]] = {}
    for case in keep:
        if case.class_name:
            keep_by_class.setdefault(case.class_name, set()).add(case.func_name)

    blocks: list[tuple[bool, list[str]]] = []  # (is_definition, source lines)
    selected = 0

    for node in tree.body:
        if _is_test_func(node):
            if node.name in keep_funcs:
                blocks.append((True, _source_block(lines, node)))
                selected += 1
            continue

        if _is_test_class(node):
            wanted = keep_by_class.get(node.name, set())
            # A class-less report (classname == module) can still target methods
            wanted = wanted or {
                m.name
                for m in node.body
                if _is_test_func(m) and m.name in keep_funcs
            }
            if not wanted:
                continue
            sliced = _slice_class(lines, node, wanted)
            if sliced:
                blocks.append((True, sliced))
                selected += 1
            continue

        is_def = isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        blocks.append((is_def, _source_block(lines, node)))

    if not selected:
        return None

    rendered = _render_blocks(blocks)
    if header_note:
        rendered = f"# {header_note}\n{rendered}"
    rendered = rendered.rstrip() + "\n"

    ast.parse(rendered)  # fail loudly rather than push a broken module
    return rendered


def _render_blocks(blocks: Sequence[tuple[bool, list[str]]]) -> str:
    """
    Join source blocks with PEP 8 spacing: two blank lines around definitions,
    single newlines between plain statements such as imports.
    """
    parts: list[str] = []
    for index, (is_def, block) in enumerate(blocks):
        text = "\n".join(block).rstrip()
        if not index:
            parts.append(text)
            continue
        prev_is_def = blocks[index - 1][0]
        # Two blank lines around definitions; runs of imports stay compact.
        gap = "\n\n\n" if (is_def or prev_is_def) else "\n"
        parts.append(gap + text)
    return "".join(parts)


def missing_tests(
    keep: Sequence[TestCaseRef],
    module_sources: dict[str, str],
) -> list[TestCaseRef]:
    """
    Cases that no longer exist in the generated modules.

    Generated files are overwritten by later pipeline runs, so a saved report
    can name tests that have since been renamed or removed.
    """
    present: set[tuple[str, str]] = set()
    for stem, source in module_sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in tree.body:
            if _is_test_func(node):
                present.add((stem, node.name))
            elif _is_test_class(node):
                for member in node.body:
                    if _is_test_func(member):
                        present.add((stem, member.name))

    known_funcs = {func for _, func in present}
    gone: list[TestCaseRef] = []
    for case in keep:
        if case.module and (case.module, case.func_name) in present:
            continue
        if not case.module and case.func_name in known_funcs:
            continue
        if case.func_name in known_funcs and case.module not in {
            stem for stem, _ in present
        }:
            # Module renamed but the test still exists — treat as present
            continue
        gone.append(case)
    return gone
