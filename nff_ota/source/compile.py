"""Compile fictional firmware source (a pseudo-`.ino`) into a mock sketch + a fault.

This is the "build" of the self-contained store. It does NOT execute the code — it scans the
source for a small catalog of genuinely buggy idioms (mirroring the crash scenarios in
``nff-mock .../scenarios.py``) and, if one is present, bakes a ``fault`` into the compiled
sketch. The mock makes that fault the source of truth for crash-looping, so the device's
behaviour is decided by the *content* of the code it holds.

The detectors are deliberately shaped so the natural fix removes the match:
  * null_deref     — a pointer assigned from a call and dereferenced with no NULL guard;
                     fixed by adding an ``if (p)`` / ``p != NULL`` check (or early return).
  * stack_overflow — a function that calls itself with no base-case ``if`` guarding the
                     recursion; fixed by adding a base case.
  * heap_leak      — an allocation inside a loop with no matching free/delete; fixed by
                     freeing (or hoisting) the allocation.

``build_id = sha256(source)[:16]`` so any edit yields a distinct image — there is no
byte-identical "re-push" that the agent would correctly no-op on (the documented Track-2 flaw).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# A pointer assigned from a function call: "Reading* r = read_sensor();", "auto *p = get_x();".
_PTR_ASSIGN = re.compile(
    r"^[ \t]*(?:[A-Za-z_][\w:<>]*[ \t]*\*|auto[ \t]*\*)[ \t]*"
    r"(?P<var>[A-Za-z_]\w*)[ \t]*=[ \t]*[A-Za-z_][\w:]*[ \t]*\(",
    re.MULTILINE,
)


def _strip_comments(source: str) -> str:
    """Drop // line and /* */ block comments so prose (e.g. a comment describing the fix)
    can't be mistaken for code. Replaced with spaces to preserve offsets/line structure."""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    source = re.sub(r"//[^\n]*", "", source)
    return source


def _has_null_guard(source: str, var: str) -> bool:
    """True if the code defends ``var`` before using it (NULL compare, truthiness, assert)."""
    v = re.escape(var)
    patterns = (
        rf"\b{v}\s*(==|!=)\s*(NULL|nullptr|0)\b",   # p != NULL / p == nullptr
        rf"\bif\s*\(\s*!?\s*{v}\s*\)",              # if (p) / if (!p)
        rf"\bassert\s*\(\s*!?\s*{v}\b",             # assert(p)
    )
    return any(re.search(p, source) for p in patterns)


def _is_dereferenced(source: str, var: str) -> bool:
    v = re.escape(var)
    return bool(re.search(rf"(?:\*\s*{v}\b|\b{v}\s*->)", source))


def _detect_null_deref(source: str) -> bool:
    for m in _PTR_ASSIGN.finditer(source):
        var = m.group("var")
        if _is_dereferenced(source, var) and not _has_null_guard(source, var):
            return True
    return False


# A function definition's name: "void loop() {", "int sensor_poll(int n) {".
_FUNC_DEF = re.compile(
    r"^[ \t]*[A-Za-z_][\w:<>\*&]*[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*\([^;{]*\)[ \t]*\{",
    re.MULTILINE,
)


def _detect_stack_overflow(source: str) -> bool:
    """A function that calls itself but has no `if` (no base case) → unbounded recursion."""
    for m in _FUNC_DEF.finditer(source):
        name = m.group("name")
        body = _function_body(source, m.end() - 1)
        if body is None:
            continue
        calls_self = re.search(rf"\b{re.escape(name)}\s*\(", body)
        has_base_case = re.search(r"\bif\s*\(", body)
        if calls_self and not has_base_case:
            return True
    return False


def _detect_heap_leak(source: str) -> bool:
    """An allocation inside a loop with no free/delete anywhere → unbounded heap growth."""
    has_loop = re.search(r"\b(for|while)\s*\(", source)
    allocates = re.search(r"\b(malloc|calloc|realloc|strdup)\s*\(|\bnew\b", source)
    frees = re.search(r"\b(free|delete)\b", source)
    return bool(has_loop and allocates and not frees)


def _function_body(source: str, open_brace_idx: int) -> str | None:
    """Return the text between a function's matching braces, starting at ``open_brace_idx``."""
    depth = 0
    for i in range(open_brace_idx, len(source)):
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace_idx + 1 : i]
    return None


# Highest-priority detector first; the first match wins. Scenario ids MUST be valid
# nff-mock scenarios (the mock validates them on load and reports their panic dump).
_DETECTORS: tuple[tuple[str, Any], ...] = (
    ("null_deref", _detect_null_deref),
    ("stack_overflow", _detect_stack_overflow),
    ("heap_leak", _detect_heap_leak),
)


def detect_fault(source: str) -> dict[str, str] | None:
    """Return {"scenario": <id>} for the first buggy idiom found, or None for clean code.

    Detection runs over the code with comments stripped, so a comment that mentions a NULL
    check / base case never masks (or fabricates) a bug."""
    code = _strip_comments(source)
    for scenario, detector in _DETECTORS:
        if detector(code):
            return {"scenario": scenario}
    return None


def build_id_for(source: str) -> str:
    """Stable 16-hex build id (a SHA-256 prefix, like the SDK's NFF_BUILD_ID)."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def compile_sketch(
    source: str, *, version: str = "1.0.0"
) -> tuple[dict[str, Any], dict[str, str] | None, str]:
    """Compile fictional source into (sketch, fault, build_id).

    ``sketch`` is the JSON the mock loads via ``load_firmware`` — it carries the version and,
    when the source is buggy, a ``fault`` block the device crash-loops on. ``fault`` is the
    same block (or None). ``build_id`` is the content hash.
    """
    fault = detect_fault(source)
    sketch: dict[str, Any] = {"version": version}
    if fault:
        sketch["fault"] = fault
    return sketch, fault, build_id_for(source)
