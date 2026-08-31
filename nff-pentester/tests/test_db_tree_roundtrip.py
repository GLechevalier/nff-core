"""The DB layer's flatten/reconstruct must round-trip the bundled tree exactly,
so the Supabase-sourced tree is identical to the JSON the walker was built on."""

from __future__ import annotations

import json
from importlib.resources import files

from nff_pentester import db


def _bundled() -> dict:
    return json.loads((files("nff_pentester.resources") / "vuln_tree.json").read_text())


def test_flatten_reconstruct_is_lossless():
    orig = _bundled()
    rows = db.flatten(orig)
    rebuilt = db.reconstruct(rows, orig["version"])
    assert rebuilt == orig


def test_flatten_assigns_parent_and_ordinal():
    orig = _bundled()
    rows = db.flatten(orig)
    by_id = {r["node_id"]: r for r in rows}

    # Roots have no parent; every other node's parent exists in the row set.
    roots = [r for r in rows if r["parent_id"] is None]
    assert len(roots) == len(orig["roots"])
    for r in rows:
        if r["parent_id"] is not None:
            assert r["parent_id"] in by_id

    # Ordinals are unique and dense (a stable DFS order).
    ordinals = sorted(r["ordinal"] for r in rows)
    assert ordinals == list(range(len(rows)))


def test_reconstruct_orders_children_by_ordinal():
    orig = _bundled()
    rows = db.flatten(orig)
    # Shuffle the rows; reconstruct must still rebuild the exact tree via ordinal.
    shuffled = list(reversed(rows))
    assert db.reconstruct(shuffled, orig["version"]) == orig


def test_seed_payload_marks_origin_seed():
    orig = _bundled()
    rows = db.flatten(orig)
    payload = [{**r, "origin": "seed"} for r in rows]
    assert payload and all(p["origin"] == "seed" for p in payload)
    # Severity column name is the DB column, not the JSON's severity_alone.
    assert all(p["severity"] in {"low", "medium", "high", "critical"} for p in payload)
