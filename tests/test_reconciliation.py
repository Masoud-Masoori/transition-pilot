"""Smoke tests for the deterministic reconciliation engine.

Goal: verify each of the 5 hard-coded patterns fires on its target case AND does
NOT fire on cases it shouldn't. This is the demo-reliability spine — if these
tests pass, the engine is ready for the demo regardless of LLM behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from transition_pilot.fhir_client import load_local_bundle
from transition_pilot.reconciliation import (
    detect_allergy_conflict,
    detect_duplicate_opioid,
    detect_hf_no_followup,
    detect_insulin_no_glucose,
    detect_warfarin_antibiotic,
    run_all,
)

CASES_DIR = Path(__file__).resolve().parents[1] / "src" / "transition_pilot" / "cases"


def _bundle(name: str) -> dict:
    return load_local_bundle(str(CASES_DIR / f"{name}.json"))


def test_warfarin_antibiotic_fires_on_target_case():
    bundle = _bundle("ahrq_warfarin_tmp_smx")
    findings = detect_warfarin_antibiotic(bundle)
    assert len(findings) == 1
    f = findings[0]
    assert f.pattern_id == "warfarin_antibiotic"
    assert f.severity.value == "high"
    cited = {f"{r.resource_type}/{r.resource_id}" for r in f.logic_link}
    assert any("MedicationRequest/mr-warfarin-1" in c for c in cited)
    assert any("MedicationRequest/mr-tmp-smx-1" in c for c in cited)


def test_warfarin_antibiotic_does_not_fire_when_no_warfarin():
    bundle = _bundle("case_2_duplicate_opioid")
    assert detect_warfarin_antibiotic(bundle) == []


def test_duplicate_opioid_fires_on_target_case():
    bundle = _bundle("case_2_duplicate_opioid")
    findings = detect_duplicate_opioid(bundle)
    assert len(findings) == 1
    f = findings[0]
    assert f.pattern_id == "duplicate_opioid"
    assert "2 active opioid" in f.title or "active opioid" in f.title


def test_duplicate_opioid_does_not_fire_on_warfarin_case():
    bundle = _bundle("ahrq_warfarin_tmp_smx")
    assert detect_duplicate_opioid(bundle) == []


def test_insulin_no_glucose_fires_on_target_case():
    bundle = _bundle("case_3_insulin_no_glucose")
    findings = detect_insulin_no_glucose(bundle)
    assert len(findings) == 1
    assert findings[0].pattern_id == "insulin_no_glucose"


def test_hf_no_followup_fires_on_target_case():
    bundle = _bundle("case_4_hf_no_followup")
    findings = detect_hf_no_followup(bundle)
    assert len(findings) == 1
    assert findings[0].pattern_id == "hf_no_followup"


def test_allergy_conflict_fires_on_target_case():
    bundle = _bundle("case_5_allergy_conflict")
    findings = detect_allergy_conflict(bundle)
    assert len(findings) >= 1
    assert any(f.pattern_id == "allergy_conflict" for f in findings)


def test_allergy_conflict_includes_allergy_evidence():
    bundle = _bundle("case_5_allergy_conflict")
    findings = detect_allergy_conflict(bundle)
    refs = findings[0].logic_link
    types = {r.resource_type for r in refs}
    assert "AllergyIntolerance" in types
    assert "MedicationRequest" in types


def test_run_all_fires_warfarin_on_demo_case():
    bundle = _bundle("ahrq_warfarin_tmp_smx")
    findings = run_all(bundle)
    pattern_ids = [f.pattern_id for f in findings]
    assert "warfarin_antibiotic" in pattern_ids


def test_run_all_handles_each_target_case_without_crashing():
    cases = [
        "ahrq_warfarin_tmp_smx",
        "case_2_duplicate_opioid",
        "case_3_insulin_no_glucose",
        "case_4_hf_no_followup",
        "case_5_allergy_conflict",
    ]
    for c in cases:
        bundle = _bundle(c)
        findings = run_all(bundle)
        assert len(findings) >= 1, f"{c} should produce at least one finding"


def test_no_findings_have_empty_logic_link():
    """Every finding must cite at least one FHIR resource."""
    cases = [
        "ahrq_warfarin_tmp_smx",
        "case_2_duplicate_opioid",
        "case_5_allergy_conflict",
    ]
    for c in cases:
        bundle = _bundle(c)
        for f in run_all(bundle):
            assert len(f.logic_link) >= 1, f"{c}: {f.pattern_id} has empty logic_link"
