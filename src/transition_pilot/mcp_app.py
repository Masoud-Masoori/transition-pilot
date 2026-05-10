"""FastMCP-compliant MCP server wrapper.

Wraps TransitionPilot's existing reconciliation + synthesis engine in a real
MCP-protocol server, served at /mcp on the same host as the demo UI. This is
what the Prompt Opinion platform calls into during a chat session.

The platform passes FHIR context as request headers per the SHARP-on-MCP
spec (https://github.com/prompt-opinion/po-community-mcp):
  - x-fhir-server-url: base URL for the FHIR server
  - x-fhir-access-token: bearer token
  - x-patient-id: current patient id

For local-dev / demo use we also accept an optional `case_id` argument which
loads one of the bundled synthetic cases from `cases/`. This is the same path
the REST `/demo/run` endpoint uses, but exposed as an MCP tool argument.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextvars import ContextVar
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

from .fhir_client import FhirClient, load_local_bundle
from .reconciliation import run_all
from .schemas import AuditTask, DischargeFailurePreventedMemo, FhirReference
from .synthesis import select_provider, synthesize_memo

log = logging.getLogger("transition_pilot.mcp")

CASES_DIR = Path(__file__).resolve().parent / "cases"

mcp = FastMCP(
    name="TransitionPilot",
    instructions=(
        "FHIR-native Specialist Auditor. Catches discharge failures (drug-drug "
        "interactions, duplicate opioids, insulin without monitoring, HF without "
        "follow-up, allergy conflicts) using a deterministic rule engine, and "
        "returns a CIO-shippable Discharge Failure Prevented memo with clickable "
        "FHIR Logic-Link evidence on every recommendation. Evidence-or-Null "
        "safety rule enforced — no recommendation without an FHIR resource ID."
    ),
)


def _stub_memo(bundle, findings, patient_id, encounter_id):
    """Used when no LLM provider key is set."""
    return DischargeFailurePreventedMemo(
        patient_id=patient_id,
        encounter_id=encounter_id,
        failures_prevented=findings,
        medication_changes=[],
        audit_tasks=[],
        clinician_summary_markdown=(
            f"# Discharge Failure Prevented memo (stub)\n\n"
            f"Patient: {patient_id}\n\n"
            f"Detected findings: {len(findings)}\n\n"
            + "\n".join(f"- **{f.title}** — {f.summary}" for f in findings)
        ),
        patient_instructions_markdown=(
            "Please review the medication list with your pharmacist before leaving."
        ),
        confidence_label="needs_review",
    )


def _audit_tasks_from_findings(findings):
    return [
        AuditTask(
            title=f.suggested_action,
            owner=f.suggested_owner,
            due_within_hours=24 if f.severity.value == "high" else 72,
            rationale=f.title,
            logic_link=f.logic_link,
        )
        for f in findings
    ]


@mcp.tool(
    name="build_transition_packet",
    description=(
        "Build a Discharge Failure Prevented memo from the current patient's "
        "FHIR R4 bundle. Detects 5 high-risk discharge patterns deterministically "
        "(anticoagulant + interacting antibiotic, duplicate opioid, insulin without "
        "glucose monitoring, heart-failure discharge without 14-day follow-up, "
        "allergy/medication conflict). Each finding is FHIR-cited via logic_link. "
        "An LLM synthesis layer writes clinician summary, patient instructions, "
        "and an optional Spanish caregiver translation. Evidence-or-Null safety "
        "rule prevents the LLM from inventing FHIR resource IDs."
    ),
)
async def build_transition_packet(
    patient_id: str | None = None,
    encounter_id: str | None = None,
    instruction_style: str = "patient_friendly",
    case_id: str | None = None,
) -> dict:
    """Returns the full memo as a dict (the platform serializes it to JSON)."""
    bundle: dict
    final_patient_id: str
    final_encounter_id: str | None = encounter_id

    if case_id:
        case_path = CASES_DIR / f"{case_id}.json"
        if not case_path.exists():
            available = ", ".join(sorted(p.stem for p in CASES_DIR.glob("*.json")))
            raise ValueError(f"unknown case_id '{case_id}'. Available: {available}")
        bundle = load_local_bundle(str(case_path))
        final_patient_id = (bundle.get("Patient") or [{}])[0].get("id") or "demo-patient"
        final_encounter_id = (bundle.get("Encounter") or [{}])[0].get("id") or None
    else:
        try:
            req = get_http_request()
            base_url = req.headers.get("x-fhir-server-url")
            token = req.headers.get("x-fhir-access-token")
            header_patient = req.headers.get("x-patient-id")
        except Exception:
            base_url = token = header_patient = None

        final_patient_id = patient_id or header_patient or ""
        if not final_patient_id:
            raise ValueError(
                "missing patient context. Provide patient_id arg or platform must "
                "pass x-patient-id header."
            )
        if not base_url or not token:
            raise ValueError(
                "missing FHIR context. Platform must pass x-fhir-server-url and "
                "x-fhir-access-token headers, or pass case_id for offline demo."
            )
        client = FhirClient(base_url, token)
        bundle = await client.fetch_patient_bundle(final_patient_id)

    findings = run_all(bundle)
    used_fallback = False
    notes: list[str] = []
    if not findings:
        notes.append("No high-risk patterns triggered.")

    provider = select_provider()
    if provider == "stub":
        memo = _stub_memo(bundle, findings, final_patient_id, final_encounter_id)
        used_fallback = True
        notes.append("No LLM provider key set — stub mode (no narrative synthesis).")
    else:
        try:
            memo, _, used_fallback = await synthesize_memo(
                bundle, findings, final_patient_id, final_encounter_id,
            )
        except Exception as e:
            log.exception("synthesis failed")
            memo = _stub_memo(bundle, findings, final_patient_id, final_encounter_id)
            used_fallback = True
            notes.append(f"synthesis failed: {type(e).__name__}: {e}")

    memo.audit_tasks = _audit_tasks_from_findings(findings)
    return {
        "memo": memo.model_dump(mode="json"),
        "used_evidence_or_null_fallback": used_fallback,
        "notes": notes,
    }


@mcp.tool(
    name="list_demo_cases",
    description=(
        "List the bundled synthetic FHIR cases that can be passed to "
        "build_transition_packet via the case_id argument. Useful when the "
        "platform is running offline or for trying TransitionPilot before "
        "wiring real FHIR context."
    ),
)
def list_demo_cases() -> list[dict]:
    """Returns case metadata."""
    out: list[dict] = []
    for p in sorted(CASES_DIR.glob("*.json")):
        out.append({
            "case_id": p.stem,
            "story": _case_story(p.stem),
        })
    return out


_CASE_STORIES = {
    "ahrq_warfarin_tmp_smx":
        "67yo F on warfarin discharged on TMP-SMX with no INR follow-up — modeled on AHRQ WebM&M.",
    "case_2_duplicate_opioid":
        "Post-op patient with two simultaneous active opioids at discharge.",
    "case_3_insulin_no_glucose":
        "Newly insulin-dependent diabetic with no glucose monitoring task.",
    "case_4_hf_no_followup":
        "HFrEF discharge with no 14-day follow-up appointment.",
    "case_5_allergy_conflict":
        "Penicillin-allergic patient discharged on amoxicillin.",
}


def _case_story(case_id: str) -> str:
    return _CASE_STORIES.get(case_id, "synthetic case bundle")
