"""Compile fictional firmware source (a pseudo-`.ino`) into a mock sketch + a fault.

This is the "build" of the self-contained store. It does NOT execute the code — it scans the
source for a small catalog of genuinely buggy idioms (mirroring the crash scenarios in
``nff-mock .../scenarios.py``) and, if one is present, bakes a ``fault`` into the compiled
sketch. The mock makes that fault the source of truth for crash-looping, so the device's
behaviour is decided by the *content* of the code it holds.

The detectors are deliberately shaped so the natural fix removes the match:
  * null_deref       — a pointer assigned from a call and dereferenced with no NULL guard;
                       fixed by adding an ``if (p)`` / ``p != NULL`` check (or early return).
  * stack_overflow   — a function that calls itself with no base-case ``if`` guarding the
                       recursion; fixed by adding a base case.
  * heap_leak        — an allocation inside a loop with no matching free/delete; fixed by
                       freeing (or hoisting) the allocation.
  * divide_by_zero   — a ``/`` or ``%`` by a variable with no zero-guard; fixed by guarding
                       the divisor (``if (n)`` / ``n != 0``).
  * unaligned_access — a wide-integer cast of a byte pointer at an offset
                       (``*(uint32_t*)(buf + n)``); fixed by using ``memcpy`` / an aligned read.
  * assert_failed    — a guaranteed-fail ``abort()`` / ``assert(false)``; fixed by removing it
                       and correcting the invariant it guarded.

These five (null_deref, stack_overflow, divide_by_zero, unaligned_access, assert_failed) are
the ids the fault-taxonomy campaign scores; each equals a nff-mock scenario AND a
nff-diagnosis ``CrashClass`` so the diagnosed class matches the injected bug.

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


# A division/modulo whose left operand is a value (word char or close-paren) and whose
# divisor is an identifier: "sum / count", "x % n". The leading [)\w] excludes printf
# format specifiers like "%d" (preceded by a quote) and compound "/=" (no ident after).
_DIVISION = re.compile(r"[)\w]\s*[/%]\s*(?P<den>[A-Za-z_]\w*)\b")


def _has_zero_guard(source: str, var: str) -> bool:
    """True if the code checks ``var`` against zero before dividing by it."""
    v = re.escape(var)
    patterns = (
        rf"\b{v}\s*(==|!=)\s*0\b",                 # n != 0 / n == 0
        rf"\bif\s*\(\s*!?\s*{v}\s*\)",             # if (n) / if (!n)
        rf"\b{v}\s*(>|>=|<|<=)\s*0\b",             # n > 0
    )
    return any(re.search(p, source) for p in patterns)


def _detect_divide_by_zero(source: str) -> bool:
    """A ``/`` or ``%`` by a variable with no zero-guard anywhere → possible div-by-zero."""
    # Drop preprocessor lines first: include paths ("#include <driver/gpio.h>") contain '/'.
    code = re.sub(r"(?m)^[ \t]*#.*$", "", source)
    for m in _DIVISION.finditer(code):
        if not _has_zero_guard(code, m.group("den")):
            return True
    return False


# A wide-integer cast of a byte/offset pointer then dereferenced: "*(uint32_t*)(buf + 1)".
_UNALIGNED = re.compile(
    r"\(\s*u?int(?:16|32|64)_t\s*\*\s*\)\s*\(?\s*[A-Za-z_]\w*\s*\+",
)


def _detect_unaligned_access(source: str) -> bool:
    """A wide-int cast of ``ptr + offset`` → likely misaligned access."""
    return bool(_UNALIGNED.search(source))


# A guaranteed-fail trap the firmware ships: a bare abort() or assert(false)/assert(0).
_FORCED_ABORT = re.compile(
    r"\babort\s*\(\s*\)"
    r"|\b(?:config)?ASSERT\s*\(\s*(?:0|false)\s*\)"
    r"|\bassert\s*\(\s*(?:0|false)\s*\)",
    re.IGNORECASE,
)


def _detect_assert_failed(source: str) -> bool:
    """A shipped abort()/assert(false) that always fires."""
    return bool(_FORCED_ABORT.search(source))


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
    ("assert_failed", _detect_assert_failed),
    ("unaligned_access", _detect_unaligned_access),
    ("divide_by_zero", _detect_divide_by_zero),
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
