"""TransitionPilot MCP server.

Single endpoint: POST /tools/build_transition_packet.

The Prompt Opinion platform passes FHIR context as request headers
(X-FHIR-Server-URL, X-FHIR-Access-Token, X-Patient-Id). For local development
and demo we also accept a `case_id` body field which loads a pre-built bundle
from the cases/ directory.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .fhir_client import FhirClient, load_local_bundle
from .mcp_app import mcp as mcp_server
from .reconciliation import run_all
from .schemas import (
    AuditTask,
    DischargeFailurePreventedMemo,
    FhirReference,
    TransitionRequest,
    TransitionResponse,
)
from .synthesis import synthesize_memo

load_dotenv()
log = logging.getLogger("transition_pilot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CASES_DIR = Path(__file__).resolve().parent / "cases"
DEMO_UI_DIR = Path(__file__).resolve().parents[2] / "demo" / "ui"

# Build FastMCP's ASGI app FIRST so we can hand its lifespan to FastAPI.
# Without lifespan integration, FastMCP's StreamableHTTPSessionManager fails
# with "task group not initialized" on every /mcp request.
mcp_streamable_app = mcp_server.http_app(transport="streamable-http", path="/")

app = FastAPI(
    title="TransitionPilot",
    version="0.1.0",
    description="FHIR-native Specialist Auditor — discharge failures prevented with FHIR provenance.",
    lifespan=mcp_streamable_app.lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DemoRequest(BaseModel):
    """Local-dev / demo flavor of the request — points at a cases/*.json bundle."""
    case_id: str
    instruction_style: str = "patient_friendly"


@app.get("/health")
def health():
    return {"status": "ok", "service": "transition-pilot", "version": "0.1.0"}


@app.get("/tools")
def list_tools():
    """MCP tool listing. Lets the host platform discover what we expose."""
    return {
        "tools": [
            {
                "name": "build_transition_packet",
                "description": "Produce a Discharge Failure Prevented memo from FHIR R4 patient context.",
                "input_schema": TransitionRequest.model_json_schema(),
            }
        ]
    }


@app.post("/tools/build_transition_packet", response_model=TransitionResponse)
async def build_transition_packet(
    payload: TransitionRequest,
    x_fhir_server_url: str | None = Header(default=None, alias="X-FHIR-Server-URL"),
    x_fhir_access_token: str | None = Header(default=None, alias="X-FHIR-Access-Token"),
    x_patient_id: str | None = Header(default=None, alias="X-Patient-Id"),
):
    t0 = time.perf_counter()
    patient_id = payload.patient_id or x_patient_id
    if not patient_id:
        raise HTTPException(400, "Missing patient context (X-Patient-Id header or patient_id body)")
    if not x_fhir_server_url or not x_fhir_access_token:
        raise HTTPException(400, "Missing FHIR context headers")

    fhir = FhirClient(x_fhir_server_url, x_fhir_access_token)
    fetch_t0 = time.perf_counter()
    bundle = await fhir.fetch_patient_bundle(patient_id)
    fetch_ms = int((time.perf_counter() - fetch_t0) * 1000)

    return await _run_pipeline(bundle, patient_id, payload.encounter_id, fetch_ms, t0)


@app.post("/demo/run", response_model=TransitionResponse)
async def demo_run(payload: DemoRequest):
    """Local-dev endpoint — loads one of the cases/*.json bundles."""
    case_path = CASES_DIR / f"{payload.case_id}.json"
    if not case_path.exists():
        raise HTTPException(404, f"Unknown case_id: {payload.case_id}")

    t0 = time.perf_counter()
    bundle = load_local_bundle(str(case_path))
    fetch_ms = int((time.perf_counter() - t0) * 1000)

    patient_id = (bundle.get("Patient") or [{}])[0].get("id") or "demo-patient"
    encounter_id = (bundle.get("Encounter") or [{}])[0].get("id")

    return await _run_pipeline(bundle, patient_id, encounter_id, fetch_ms, t0)


@app.get("/demo/cases")
def demo_cases():
    """List available demo cases."""
    if not CASES_DIR.exists():
        return {"cases": []}
    return {"cases": [p.stem for p in sorted(CASES_DIR.glob("*.json"))]}


@app.get("/demo/cases/{case_id}.json")
def demo_case_raw(case_id: str):
    """Serve raw FHIR bundle JSON for evidence-panel hydration."""
    p = CASES_DIR / f"{case_id}.json"
    if not p.exists():
        raise HTTPException(404, f"Unknown case_id: {case_id}")
    return FileResponse(p, media_type="application/fhir+json")


# Mount demo UI at /demo/ui — operator runs `python -m transition_pilot.server`
# and points a browser at http://127.0.0.1:8089/demo/ui/ for the live demo.
if DEMO_UI_DIR.exists():
    app.mount("/demo/ui", StaticFiles(directory=str(DEMO_UI_DIR), html=True), name="demo-ui")


# Mount the FastMCP streamable-http app at /mcp.
# This is what the Prompt Opinion platform connects to during a chat session.
# The REST endpoints above stay for the demo UI and local dev.
app.mount("/mcp", mcp_streamable_app)


async def _run_pipeline(
    bundle: dict,
    patient_id: str,
    encounter_id: str | None,
    fetch_ms: int,
    t0: float,
) -> TransitionResponse:
    """The four-stage pipeline: detect → synthesize → enrich → return."""
    findings = run_all(bundle)
    notes: list[str] = []

    if not findings:
        notes.append("No high-risk patterns triggered. Memo will be brief.")

    has_provider = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not has_provider:
        memo = _stub_memo(bundle, findings, patient_id, encounter_id)
        used_fallback = True
        notes.append("No LLM provider key set (ANTHROPIC_API_KEY or GROQ_API_KEY) — stub mode.")
    else:
        try:
            memo, _, used_fallback = await synthesize_memo(
                bundle, findings, patient_id, encounter_id,
            )
        except Exception as e:
            log.exception("Synthesis failed — falling back to stub memo")
            memo = _stub_memo(bundle, findings, patient_id, encounter_id)
            used_fallback = True
            notes.append(f"Synthesis failed: {type(e).__name__}: {e}")

    memo.audit_tasks = _build_audit_tasks(findings)

    total_ms = int((time.perf_counter() - t0) * 1000)
    return TransitionResponse(
        memo=memo,
        timing={"fetch_ms": fetch_ms, "total_ms": total_ms},
        used_evidence_or_null_fallback=used_fallback,
        notes=notes,
    )


def _build_audit_tasks(findings):
    """Convert each detected failure into one actionable Task."""
    tasks: list[AuditTask] = []
    for f in findings:
        tasks.append(AuditTask(
            title=f.suggested_action,
            owner=f.suggested_owner,
            due_within_hours=24 if f.severity.value == "high" else 72,
            rationale=f.title,
            logic_link=f.logic_link,
        ))
    return tasks


def _stub_memo(bundle, findings, patient_id, encounter_id) -> DischargeFailurePreventedMemo:
    """Structural-only memo for offline / no-API-key mode."""
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
            "Please review the medication list with your pharmacist before leaving the hospital."
        ),
        confidence_label="needs_review",
    )


def main():
    import uvicorn
    port = int(os.environ.get("PORT", "8089"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
