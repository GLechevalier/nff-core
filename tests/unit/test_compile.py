"""Unit tests for the fictional-firmware compiler (nff_ota.source.compile).

Pure, no DB: the compiler is a static analyzer over pseudo-`.ino` source. The contract that
makes the self-healing demo honest is that a buggy idiom compiles to a fault and the natural
fix removes it, with a content-derived build id that always changes on an edit.
"""

from __future__ import annotations

from nff_ota.source.compile import compile_sketch, detect_fault

# The seeded buggy firmware: a pointer from a call dereferenced with no NULL guard.
BUGGY = """
#include "nff.h"
Reading* read_sensor();
void publish(int celsius);

void loop() {
  Reading* r = read_sensor();
  publish(r->celsius);   // no NULL check
  delay(1000);
}
"""

# The natural fix: guard the dereference.
FIXED = """
#include "nff.h"
Reading* read_sensor();
void publish(int celsius);

void loop() {
  Reading* r = read_sensor();
  if (r == NULL) { delay(1000); return; }
  publish(r->celsius);
  delay(1000);
}
"""


def test_buggy_source_compiles_to_null_deref_fault():
    sketch, fault, build_id = compile_sketch(BUGGY, version="5.0.0")
    assert fault == {"scenario": "null_deref"}
    assert sketch["fault"] == {"scenario": "null_deref"}
    assert sketch["version"] == "5.0.0"
    assert len(build_id) == 16


def test_fixed_source_has_no_fault():
    sketch, fault, _ = compile_sketch(FIXED, version="5.0.1")
    assert fault is None
    assert "fault" not in sketch


def test_truthiness_guard_also_clears_fault():
    src = BUGGY.replace("publish(r->celsius);", "if (r) publish(r->celsius);")
    assert detect_fault(src) is None


def test_build_id_changes_with_content():
    _, _, a = compile_sketch(BUGGY)
    _, _, b = compile_sketch(FIXED)
    assert a != b  # no byte-identical no-op re-push


def test_stack_overflow_detected():
    src = "int recurse(int n) { return recurse(n + 1); }"
    assert detect_fault(src) == {"scenario": "stack_overflow"}
    # A base case clears it.
    fixed = "int recurse(int n) { if (n > 10) return n; return recurse(n + 1); }"
    assert detect_fault(fixed) is None


def test_heap_leak_detected():
    src = "void loop() { while (1) { char* b = (char*)malloc(64); use(b); } }"
    assert detect_fault(src) == {"scenario": "heap_leak"}
    fixed = "void loop() { while (1) { char* b = (char*)malloc(64); use(b); free(b); } }"
    assert detect_fault(fixed) is None


def test_clean_trivial_source_has_no_fault():
    assert detect_fault("void loop() { delay(1000); }") is None
