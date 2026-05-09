"""Pydantic models for the TransitionPilot MCP server.

Every output object that names a clinical fact MUST carry a `logic_link` list of
FhirReference objects. This is the structural enforcement of the Evidence-or-Null
rule from the council synthesis: an output without a logic link is impossible to
construct, which means the LLM cannot emit a recommendation without citing
evidence — or it must explicitly mark the recommendation as provider-directed.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class FhirReference(BaseModel):
    """Pointer to a FHIR resource that supports a finding."""
    resource_type: str = Field(description="e.g. MedicationRequest, Condition, Observation")
    resource_id: str = Field(description="The FHIR id of the resource")
    note: str | None = Field(default=None, description="One-line plain-English why this resource is cited")


class FailureSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExposureBand(str, Enum):
    """Coarse financial-exposure label. We refuse to emit precision we don't have."""
    LOW = "low"             # < $5k typical avoidable cost
    MEDIUM = "medium"       # $5-15k
    HIGH = "high"           # > $15k (readmission territory)


class MedicationAction(str, Enum):
    CONTINUE = "continue"
    HELD = "held"
    NEW = "new"
    STOPPED = "stopped"
    UNCLEAR = "unclear"


class MedicationChange(BaseModel):
    """One row of the discharge medication table. Must cite at least one FHIR resource."""
    name: str
    rxnorm_code: str | None = None
    action: MedicationAction
    reason: str = Field(description="Plain-English why. If LLM can't cite, say 'Provider-Directed (No FHIR Link Found)'.")
    logic_link: list[FhirReference] = Field(
        default_factory=list,
        description="FHIR resources that drove this decision. Empty = provider-directed.",
    )


class AuditTask(BaseModel):
    """A follow-up task the team must own. Maps cleanly to FHIR Task on export."""
    title: str
    owner: str = Field(description="Pharmacist | RN | Hospitalist | Care Coordinator | Patient")
    due_within_hours: int
    rationale: str
    logic_link: list[FhirReference] = Field(default_factory=list)


class DischargeFailure(BaseModel):
    """The headline finding. One per detected pattern."""
    pattern_id: str = Field(description="One of: warfarin_antibiotic, duplicate_opioid, insulin_no_glucose, hf_no_followup, allergy_conflict")
    title: str
    severity: FailureSeverity
    exposure_band: ExposureBand
    summary: str
    logic_link: list[FhirReference]
    suggested_owner: str
    suggested_action: str


class DischargeFailurePreventedMemo(BaseModel):
    """The CIO-shippable headline artifact. This is the demo image."""
    patient_id: str
    encounter_id: str | None = None
    failures_prevented: list[DischargeFailure]
    medication_changes: list[MedicationChange]
    audit_tasks: list[AuditTask]
    clinician_summary_markdown: str
    patient_instructions_markdown: str
    patient_instructions_es_markdown: str | None = Field(
        default=None,
        description="Spanish caregiver version. Mentioned in 1 line of the demo, not its own beat.",
    )
    confidence_label: Literal["evidence_grounded", "needs_review"] = "evidence_grounded"
    runtime_ms: int | None = None


class TransitionRequest(BaseModel):
    """MCP tool input."""
    patient_id: str | None = None
    encounter_id: str | None = None
    instruction_style: Literal["patient_friendly", "clinician_handoff"] = "patient_friendly"


class TransitionResponse(BaseModel):
    """MCP tool output. The single envelope the platform consumes."""
    memo: DischargeFailurePreventedMemo
    timing: dict[str, int] = Field(default_factory=dict, description="Per-stage milliseconds")
    used_evidence_or_null_fallback: bool = False
    notes: list[str] = Field(default_factory=list)
