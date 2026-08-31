"""The taxonomy registry must stay in lockstep with the CrashClass literal."""

from typing import get_args

from nff_diagnosis.schemas.diagnosis_output import CrashClass
from nff_diagnosis.schemas.taxonomy import FAULT_TAXONOMY, taxonomy_for


def test_every_crash_class_has_taxonomy_entry():
    for cls in get_args(CrashClass):
        assert cls in FAULT_TAXONOMY, f"{cls} missing from FAULT_TAXONOMY"


def test_no_stray_taxonomy_keys():
    valid = set(get_args(CrashClass))
    for key in FAULT_TAXONOMY:
        assert key in valid, f"{key} is not a CrashClass"


def test_entry_key_matches_dict_key():
    for key, entry in FAULT_TAXONOMY.items():
        assert entry.key == key


def test_watchdog_is_the_symptom_layer():
    # This is the load-bearing flag for "symptom-masking fails" scoring downstream.
    assert FAULT_TAXONOMY["watchdog"].is_symptom is True
    assert taxonomy_for("null_deref").is_symptom is False


def test_taxonomy_for_falls_back_to_unknown():
    assert taxonomy_for("does_not_exist").key == "unknown"  # type: ignore[arg-type]
