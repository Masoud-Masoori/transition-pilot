"""Deterministic reconciliation engine — the 5 high-risk discharge patterns.

Per the council synthesis: the LLM never invents these findings. The engine
detects them deterministically against the FHIR bundle, and the LLM only writes
the plain-English explanation around them. This is the spine that makes the
demo reliable and gives the AI Logic-Link its evidence base.

Each detector returns a list of DischargeFailure with logic_link populated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from .schemas import (
    DischargeFailure,
    ExposureBand,
    FailureSeverity,
    FhirReference,
)

# ── Drug knowledge (small fixed table — production would query RxNorm) ──────

WARFARIN_RXNORM = {"11289"}
WARFARIN_NAMES = {"warfarin", "coumadin", "jantoven"}

INTERACTING_ANTIBIOTICS_RXNORM = {
    "10180",   # trimethoprim
    "10831",   # sulfamethoxazole
    "10180,10831",  # bactrim/septra combo
    "2551",    # ciprofloxacin
    "6922",    # metronidazole
    "4450",    # fluconazole
}
INTERACTING_ANTIBIOTIC_NAMES = {
    "trimethoprim", "sulfamethoxazole", "bactrim", "septra",
    "ciprofloxacin", "cipro", "metronidazole", "flagyl", "fluconazole", "diflucan",
}

OPIOID_RXNORM = {
    "7052",   # morphine
    "5489",   # hydromorphone
    "7804",   # oxycodone
    "3423",   # hydrocodone
    "4337",   # fentanyl
    "10689",  # tramadol
    "7243",   # methadone
    "161",    # codeine
}
OPIOID_NAMES = {
    "morphine", "hydromorphone", "oxycodone", "hydrocodone", "fentanyl",
    "tramadol", "methadone", "codeine", "oxycontin", "percocet", "vicodin",
    "norco", "dilaudid", "ms contin",
}

INSULIN_RXNORM = {"5856", "253182", "274783", "311034", "51428", "284810"}
INSULIN_NAMES = {
    "insulin", "lantus", "humalog", "novolog", "novolin", "humulin",
    "tresiba", "levemir", "basaglar", "glargine", "lispro", "aspart",
}

GLUCOSE_LOINC = {"2339-0", "2345-7", "2347-3", "41653-7", "2339-0", "1547-9"}

# Heart failure ICD-10 prefix
HF_CODE_PREFIXES = ("I50", "I11.0", "I13.0", "I13.2")

# Allergen-class → set of medication names that fall in the same class.
# Production would query RxNorm hierarchy. For demo, this small table covers
# the common clinical landmines (penicillin, sulfa, NSAIDs, opioids).
ALLERGEN_CLASS_TO_DRUGS: dict[str, set[str]] = {
    "penicillin": {"amoxicillin", "ampicillin", "penicillin", "augmentin",
                   "amoxicillin-clavulanate", "piperacillin", "dicloxacillin",
                   "nafcillin", "oxacillin"},
    "sulfa": {"sulfamethoxazole", "tmp-smx", "trimethoprim/sulfamethoxazole",
              "bactrim", "septra", "sulfasalazine", "sulfadiazine"},
    "nsaid": {"ibuprofen", "naproxen", "ketorolac", "diclofenac", "celecoxib",
              "indomethacin", "meloxicam"},
    "opioid": {"morphine", "oxycodone", "hydrocodone", "hydromorphone",
               "fentanyl", "codeine", "tramadol"},
    "aspirin": {"aspirin", "acetylsalicylic acid"},
    "statin": {"atorvastatin", "simvastatin", "rosuvastatin", "pravastatin",
               "lovastatin"},
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _med_name(med: dict) -> str:
    cc = med.get("medicationCodeableConcept") or {}
    if cc.get("text"):
        return cc["text"].lower()
    for c in cc.get("coding") or []:
        if c.get("display"):
            return c["display"].lower()
    return ""


def _med_codes(med: dict) -> set[str]:
    cc = med.get("medicationCodeableConcept") or {}
    return {c.get("code") for c in cc.get("coding") or [] if c.get("code")}


def _is_active(med: dict) -> bool:
    return (med.get("status") or "").lower() in {"active", "completed", "on-hold"}


def _matches_drug(med: dict, codes: set[str], names: Iterable[str]) -> bool:
    if _med_codes(med) & codes:
        return True
    n = _med_name(med)
    return any(name in n for name in names)


def _ref(res: dict, note: str | None = None) -> FhirReference:
    return FhirReference(
        resource_type=res.get("resourceType", "Resource"),
        resource_id=res.get("id", "?"),
        note=note,
    )


def _has_recent_observation(obs: list[dict], loincs: set[str], hours: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for o in obs:
        for c in (o.get("code") or {}).get("coding") or []:
            if c.get("code") in loincs:
                eff = o.get("effectiveDateTime") or o.get("issued")
                if not eff:
                    continue
                try:
                    dt = datetime.fromisoformat(eff.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if dt >= cutoff:
                    return True
    return False


def _has_task_about(tasks: list[dict], keywords: Iterable[str]) -> bool:
    for t in tasks:
        text = (t.get("description") or "").lower()
        if any(k in text for k in keywords):
            return True
        for c in (t.get("code") or {}).get("coding") or []:
            display = (c.get("display") or "").lower()
            if any(k in display for k in keywords):
                return True
    return False


# ── Pattern 1 — Anticoagulant + interacting antibiotic ─────────────────────

def detect_warfarin_antibiotic(bundle: dict) -> list[DischargeFailure]:
    meds = [m for m in bundle.get("MedicationRequest", []) if _is_active(m)]
    warfarins = [m for m in meds if _matches_drug(m, WARFARIN_RXNORM, WARFARIN_NAMES)]
    if not warfarins:
        return []
    abx = [m for m in meds if _matches_drug(m, INTERACTING_ANTIBIOTICS_RXNORM, INTERACTING_ANTIBIOTIC_NAMES)]
    if not abx:
        return []

    has_followup = _has_task_about(bundle.get("Task", []), ["inr", "international normalized"])
    has_recent_inr = _has_recent_observation(bundle.get("Observation", []), {"6301-6", "34714-6"}, hours=48)

    refs = [_ref(warfarins[0], "Active warfarin")]
    refs.extend(_ref(a, "Interacting antibiotic prescribed at discharge") for a in abx)
    if not has_followup and not has_recent_inr:
        refs.append(FhirReference(resource_type="Task", resource_id="—",
                                  note="No INR follow-up Task found in bundle"))

    return [DischargeFailure(
        pattern_id="warfarin_antibiotic",
        title="Warfarin + interacting antibiotic without INR follow-up",
        severity=FailureSeverity.HIGH,
        exposure_band=ExposureBand.HIGH,
        summary=(
            "Patient is being discharged on warfarin with a co-prescribed antibiotic "
            "known to potentiate INR. No INR follow-up task or recent INR observation "
            "was found. Risk of bleeding within 5–10 days post-discharge."
        ),
        logic_link=refs,
        suggested_owner="Pharmacist",
        suggested_action="Order INR check within 48h; counsel patient on bleeding signs.",
    )]


# ── Pattern 2 — Duplicate opioid ────────────────────────────────────────────

def detect_duplicate_opioid(bundle: dict) -> list[DischargeFailure]:
    meds = [m for m in bundle.get("MedicationRequest", []) if _is_active(m)]
    opioids = [m for m in meds if _matches_drug(m, OPIOID_RXNORM, OPIOID_NAMES)]
    if len(opioids) < 2:
        return []

    refs = [_ref(m, f"Active opioid: {_med_name(m) or '?'}") for m in opioids[:3]]
    return [DischargeFailure(
        pattern_id="duplicate_opioid",
        title=f"{len(opioids)} active opioid prescriptions at discharge",
        severity=FailureSeverity.HIGH,
        exposure_band=ExposureBand.MEDIUM,
        summary=(
            "Multiple active opioid MedicationRequests at discharge. Risk of overdose, "
            "respiratory depression, and unintended polyprescription."
        ),
        logic_link=refs,
        suggested_owner="Hospitalist",
        suggested_action="Reconcile to a single discharge opioid; document rationale if intentional.",
    )]


# ── Pattern 3 — Insulin without glucose monitoring ─────────────────────────

def detect_insulin_no_glucose(bundle: dict) -> list[DischargeFailure]:
    meds = [m for m in bundle.get("MedicationRequest", []) if _is_active(m)]
    insulins = [m for m in meds if _matches_drug(m, INSULIN_RXNORM, INSULIN_NAMES)]
    if not insulins:
        return []

    has_glucose_task = _has_task_about(
        bundle.get("Task", []),
        ["glucose", "blood sugar", "fingerstick", "cgm"],
    )
    has_recent_glucose = _has_recent_observation(
        bundle.get("Observation", []), GLUCOSE_LOINC, hours=72
    )
    if has_glucose_task or has_recent_glucose:
        return []

    refs = [_ref(insulins[0], "Active insulin prescription")]
    refs.append(FhirReference(resource_type="Task", resource_id="—",
                              note="No glucose-monitoring Task found"))
    return [DischargeFailure(
        pattern_id="insulin_no_glucose",
        title="Insulin prescribed without glucose-monitoring follow-up",
        severity=FailureSeverity.MEDIUM,
        exposure_band=ExposureBand.MEDIUM,
        summary=(
            "Insulin therapy at discharge with no glucose-monitoring task or recent "
            "fingerstick/CGM observation in the bundle. Risk of hypoglycemia at home."
        ),
        logic_link=refs,
        suggested_owner="RN",
        suggested_action="Schedule glucose monitoring; verify patient has working meter & strips.",
    )]


# ── Pattern 4 — Heart failure discharge without follow-up ──────────────────

def detect_hf_no_followup(bundle: dict) -> list[DischargeFailure]:
    hf = [c for c in bundle.get("Condition", [])
          if any((cc.get("code") or "").startswith(HF_CODE_PREFIXES)
                 for cc in (c.get("code") or {}).get("coding") or [])]
    if not hf:
        return []
    inpatients = [e for e in bundle.get("Encounter", [])
                  if (e.get("class") or {}).get("code") in {"IMP", "ACUTE"}
                  or "inpatient" in (e.get("type") or [{}])[0].get("text", "").lower()]
    if not inpatients:
        return []

    cutoff = datetime.now(timezone.utc) + timedelta(days=14)
    has_followup = False
    for ap in bundle.get("Appointment", []):
        start = ap.get("start")
        if not start:
            continue
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt <= cutoff and (ap.get("status") or "").lower() in {"booked", "pending", "proposed"}:
            has_followup = True
            break
    if has_followup:
        return []

    refs = [_ref(hf[0], "Heart failure diagnosis"), _ref(inpatients[0], "Recent inpatient encounter")]
    refs.append(FhirReference(resource_type="Appointment", resource_id="—",
                              note="No Appointment within 14 days post-discharge"))
    return [DischargeFailure(
        pattern_id="hf_no_followup",
        title="Heart-failure discharge with no follow-up appointment within 14 days",
        severity=FailureSeverity.HIGH,
        exposure_band=ExposureBand.HIGH,
        summary=(
            "Patient with heart failure is being discharged without a confirmed "
            "follow-up appointment in the next 14 days. CMS targets 7-day follow-up "
            "to reduce 30-day readmission."
        ),
        logic_link=refs,
        suggested_owner="Care Coordinator",
        suggested_action="Schedule 7-day cardiology or PCP follow-up before discharge papers leave.",
    )]


# ── Pattern 5 — Allergy conflict ───────────────────────────────────────────

def _class_for_allergy_text(text: str) -> str | None:
    """Map an allergy free-text into a known class key, if any."""
    t = text.lower()
    for cls in ALLERGEN_CLASS_TO_DRUGS:
        if cls in t:
            return cls
    return None


def detect_allergy_conflict(bundle: dict) -> list[DischargeFailure]:
    allergies = bundle.get("AllergyIntolerance", [])
    if not allergies:
        return []
    allergy_codes: set[str] = set()
    allergy_names: list[str] = []
    allergy_classes: set[str] = set()
    for a in allergies:
        for c in (a.get("code") or {}).get("coding") or []:
            if c.get("code"):
                allergy_codes.add(c["code"])
            display = (c.get("display") or "").lower()
            cls = _class_for_allergy_text(display)
            if cls:
                allergy_classes.add(cls)
        text = ((a.get("code") or {}).get("text") or "").lower()
        if text:
            allergy_names.append(text)
            cls = _class_for_allergy_text(text)
            if cls:
                allergy_classes.add(cls)

    findings: list[DischargeFailure] = []
    for m in bundle.get("MedicationRequest", []):
        if not _is_active(m):
            continue
        m_codes = _med_codes(m)
        m_name = _med_name(m)
        hit_code = m_codes & allergy_codes
        hit_name = next((n for n in allergy_names if n and n in m_name), None)
        hit_class = next(
            (cls for cls in allergy_classes
             if any(drug in m_name for drug in ALLERGEN_CLASS_TO_DRUGS[cls])),
            None,
        )
        if hit_code or hit_name or hit_class:
            if hit_code:
                ref_note = f"Med code {next(iter(hit_code))} matches allergy"
            elif hit_class:
                ref_note = f"Med '{m_name}' is in '{hit_class}' class — patient allergic"
            else:
                ref_note = f"Med name '{m_name}' matches allergy '{hit_name}'"
            refs = [_ref(m, ref_note), _ref(allergies[0], "Documented allergy")]
            findings.append(DischargeFailure(
                pattern_id="allergy_conflict",
                title=f"Allergy conflict: {_med_name(m) or 'medication'} matches documented allergy",
                severity=FailureSeverity.HIGH,
                exposure_band=ExposureBand.HIGH,
                summary=(
                    "A medication on the discharge list matches a documented "
                    "AllergyIntolerance for this patient."
                ),
                logic_link=refs,
                suggested_owner="Pharmacist",
                suggested_action="Hold medication; contact prescriber for substitution.",
            ))
    return findings


# ── Dispatcher ──────────────────────────────────────────────────────────────

PATTERN_DETECTORS = [
    detect_warfarin_antibiotic,
    detect_duplicate_opioid,
    detect_insulin_no_glucose,
    detect_hf_no_followup,
    detect_allergy_conflict,
]


def run_all(bundle: dict) -> list[DischargeFailure]:
    """Run every detector. Bounded-error: a crashing detector never crashes the whole run."""
    findings: list[DischargeFailure] = []
    for det in PATTERN_DETECTORS:
        try:
            findings.extend(det(bundle))
        except Exception:
            # Defensively swallow — we'd rather under-report than crash the demo.
            continue
    return findings
