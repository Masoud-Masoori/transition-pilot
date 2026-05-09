"""LLM synthesis layer.

Takes deterministic findings from `reconciliation.run_all(bundle)` and produces
the human-facing parts of the memo: clinician summary markdown, patient
instructions markdown, and an optional Spanish caregiver block.

Provider strategy (auto-selected at startup based on which key is set):
  - ANTHROPIC_API_KEY  →  Anthropic Claude (Haiku 4.5 default), tool-use schema
  - GROQ_API_KEY       →  Groq Llama 3.3 70B via OpenAI-compatible JSON mode
  - (none)             →  stub mode (deterministic findings only, no narrative)

Both providers enforce the **Evidence-or-Null** safety rule:
  1. The system prompt forbids emitting any clinical claim without an
     FHIR resource ID present in the input.
  2. After the call, every cited ID is validated against the bundle.
     Rogue IDs are stripped, confidence drops to `needs_review`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .schemas import (
    DischargeFailure,
    DischargeFailurePreventedMemo,
    MedicationChange,
)

log = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

SYSTEM_PROMPT = """You are TransitionPilot, a Specialist Auditor MCP server for hospital discharge.

Your job is NOT to summarize and NOT to predict. Your job is to convert deterministic
clinical findings (already detected by a rule engine) into:

  1. a concise clinician handoff summary (markdown)
  2. patient-friendly discharge instructions in plain English (~8th grade reading level)
  3. an optional Spanish caregiver version of the patient instructions
  4. a final medication-change table (continue / held / new / stopped / unclear)

ABSOLUTE SAFETY RULE — Evidence-or-Null:
  - Every clinical claim you emit MUST be supported by at least one FHIR resource ID
    that is present in the supplied bundle.
  - If you want to recommend something but cannot cite a specific
    MedicationRequest / Condition / Observation / AllergyIntolerance / Encounter /
    Task / Appointment ID from the bundle, you MUST mark that recommendation as
    "Provider-Directed (No FHIR Link Found)" rather than inventing a citation.
  - If the bundle is missing data you would need, say so explicitly. Do not guess.

You are not a chatbot. You are an auditor. Be concise, structured, evidence-bound."""


# ── Output schema (shared by both providers) ──────────────────────────────────

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clinician_summary_markdown": {
            "type": "string",
            "description": "Concise markdown handoff. 200-400 words. Lists each detected failure with its evidence.",
        },
        "patient_instructions_markdown": {
            "type": "string",
            "description": "Plain-English (~8th grade) discharge instructions. 200-400 words.",
        },
        "patient_instructions_es_markdown": {
            "type": "string",
            "description": "Spanish caregiver version of patient_instructions_markdown.",
        },
        "medication_changes": {
            "type": "array",
            "description": "Final reconciled discharge medication table.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "rxnorm_code": {"type": ["string", "null"]},
                    "action": {
                        "type": "string",
                        "enum": ["continue", "held", "new", "stopped", "unclear"],
                    },
                    "reason": {"type": "string"},
                    "logic_link": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "resource_type": {"type": "string"},
                                "resource_id": {"type": "string"},
                                "note": {"type": ["string", "null"]},
                            },
                            "required": ["resource_type", "resource_id"],
                        },
                    },
                },
                "required": ["name", "action", "reason", "logic_link"],
            },
        },
    },
    "required": [
        "clinician_summary_markdown",
        "patient_instructions_markdown",
        "medication_changes",
    ],
}

ANTHROPIC_TOOL = {
    "name": "emit_discharge_memo_text",
    "description": "Emit the human-readable parts of the Discharge Failure Prevented memo.",
    "input_schema": OUTPUT_SCHEMA,
}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _bundle_inventory(bundle: dict) -> str:
    """Compact representation of the bundle the LLM is allowed to cite."""
    lines = []
    for rt, items in bundle.items():
        if not items:
            continue
        lines.append(f"## {rt} ({len(items)})")
        for r in items[:25]:
            rid = r.get("id", "?")
            label = ""
            cc = r.get("medicationCodeableConcept") or r.get("code") or {}
            if isinstance(cc, dict):
                label = (cc.get("text") or
                         (cc.get("coding") or [{}])[0].get("display") or "")
            status = r.get("status") or ""
            lines.append(f"- {rid} {label} {status}".strip())
    return "\n".join(lines)


def _findings_block(findings: list[DischargeFailure]) -> str:
    out = []
    for f in findings:
        cites = ", ".join(f"{r.resource_type}/{r.resource_id}" for r in f.logic_link)
        out.append(
            f"- [{f.severity.value}] {f.title}\n"
            f"  cite: {cites}\n"
            f"  summary: {f.summary}"
        )
    return "\n".join(out) if out else "(no patterns triggered)"


def _user_prompt(bundle: dict, findings: list[DischargeFailure]) -> str:
    return f"""# Patient bundle (the only resources you may cite)

{_bundle_inventory(bundle)}

# Deterministic findings already detected (you must include these — do not invent additional ones)

{_findings_block(findings)}

# Task

Produce the structured memo. For every medication on the bundle's MedicationRequest list,
emit one row in `medication_changes` with logic_link pointing to that exact resource id.

If a finding has no logic_link evidence, set `reason` to "Provider-Directed (No FHIR Link Found)".

Keep clinician summary under 350 words. Patient instructions under 350 words.
Always include patient_instructions_es_markdown (Spanish caregiver version).
"""


def _collect_valid_ids(bundle: dict) -> set[str]:
    valid: set[str] = set()
    for rt, items in bundle.items():
        for r in items:
            rid = r.get("id")
            if rid:
                valid.add(f"{rt}/{rid}")
    return valid


def _validate_citations(items: list[dict], valid: set[str]) -> tuple[list[dict], int]:
    dropped = 0
    cleaned: list[dict] = []
    for item in items:
        ll = item.get("logic_link") or []
        kept = []
        for ref in ll:
            key = f"{ref.get('resource_type')}/{ref.get('resource_id')}"
            if key in valid or ref.get("resource_id") == "—":
                kept.append(ref)
            else:
                dropped += 1
        item["logic_link"] = kept
        cleaned.append(item)
    return cleaned, dropped


# ── Provider: Anthropic ───────────────────────────────────────────────────────

async def _call_anthropic(
    bundle: dict, findings: list[DischargeFailure], *, model: str, max_tokens: int
) -> dict[str, Any]:
    from anthropic import Anthropic
    client = Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        tools=[ANTHROPIC_TOOL],
        tool_choice={"type": "tool", "name": ANTHROPIC_TOOL["name"]},
        messages=[{"role": "user", "content": _user_prompt(bundle, findings)}],
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == ANTHROPIC_TOOL["name"]:
            return block.input  # type: ignore[return-value]
    raise RuntimeError("Anthropic returned no tool_use block")


# ── Provider: Groq (OpenAI-compatible) ────────────────────────────────────────

async def _call_groq(
    bundle: dict, findings: list[DischargeFailure], *, model: str, max_tokens: int
) -> dict[str, Any]:
    from openai import OpenAI
    client = OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=os.environ.get("GROQ_API_KEY"),
    )
    schema_hint = json.dumps(OUTPUT_SCHEMA, indent=2)
    user = _user_prompt(bundle, findings) + f"""

# Output format
Respond with a single JSON object matching this schema (no markdown fences, no prose):

```
{schema_hint}
```
"""
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    return json.loads(raw)


# ── Public entrypoint ────────────────────────────────────────────────────────

def select_provider() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return "stub"


async def synthesize_memo(
    bundle: dict,
    findings: list[DischargeFailure],
    patient_id: str,
    encounter_id: str | None,
    *,
    max_tokens: int = 2500,
) -> tuple[DischargeFailurePreventedMemo, dict[str, int], bool]:
    """Run synthesis. Returns (memo, timing_ms, used_evidence_or_null_fallback)."""
    t0 = time.perf_counter()
    provider = select_provider()

    if provider == "anthropic":
        log.info("synthesis provider=anthropic model=%s", DEFAULT_ANTHROPIC_MODEL)
        tool_input = await _call_anthropic(bundle, findings,
                                           model=DEFAULT_ANTHROPIC_MODEL,
                                           max_tokens=max_tokens)
    elif provider == "groq":
        log.info("synthesis provider=groq model=%s", DEFAULT_GROQ_MODEL)
        tool_input = await _call_groq(bundle, findings,
                                      model=DEFAULT_GROQ_MODEL,
                                      max_tokens=max_tokens)
    else:
        raise RuntimeError("No LLM provider key set (ANTHROPIC_API_KEY or GROQ_API_KEY)")

    valid_ids = _collect_valid_ids(bundle)
    med_changes_raw, dropped = _validate_citations(
        tool_input.get("medication_changes") or [], valid_ids
    )
    med_changes = [MedicationChange(**m) for m in med_changes_raw]
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    memo = DischargeFailurePreventedMemo(
        patient_id=patient_id,
        encounter_id=encounter_id,
        failures_prevented=findings,
        medication_changes=med_changes,
        audit_tasks=[],
        clinician_summary_markdown=tool_input.get("clinician_summary_markdown", ""),
        patient_instructions_markdown=tool_input.get("patient_instructions_markdown", ""),
        patient_instructions_es_markdown=tool_input.get("patient_instructions_es_markdown") or None,
        confidence_label="needs_review" if dropped > 0 else "evidence_grounded",
        runtime_ms=elapsed_ms,
    )
    return memo, {"synthesis_ms": elapsed_ms}, dropped > 0
