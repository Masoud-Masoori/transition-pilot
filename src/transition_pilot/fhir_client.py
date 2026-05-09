"""Async FHIR R4 client.

Designed for the platform's 60-second budget: parallel fetch of the resources
TransitionPilot's reconciliation engine needs, in <8s total.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class FhirClient:
    def __init__(self, base_url: str, bearer_token: str, timeout: float = 8.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/fhir+json",
        }
        self._timeout = timeout

    async def fetch_patient_bundle(self, patient_id: str) -> dict[str, list[dict]]:
        """Pull the resources needed by the 5 reconciliation patterns in parallel."""
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            results = await asyncio.gather(
                self._search(client, "MedicationRequest", patient_id, count=50),
                self._search(client, "MedicationStatement", patient_id, count=50),
                self._search(client, "AllergyIntolerance", patient_id, count=20),
                self._search(client, "Condition", patient_id, count=50),
                self._search(client, "Observation", patient_id, count=50, sort="-date"),
                self._search(client, "Encounter", patient_id, count=10, sort="-date"),
                self._search(client, "Task", patient_id, count=20),
                self._search(client, "Appointment", patient_id, count=10, sort="-date"),
                return_exceptions=True,
            )

        return {
            "MedicationRequest": _safe(results[0]),
            "MedicationStatement": _safe(results[1]),
            "AllergyIntolerance": _safe(results[2]),
            "Condition": _safe(results[3]),
            "Observation": _safe(results[4]),
            "Encounter": _safe(results[5]),
            "Task": _safe(results[6]),
            "Appointment": _safe(results[7]),
        }

    async def _search(
        self,
        client: httpx.AsyncClient,
        resource: str,
        patient_id: str,
        count: int = 50,
        sort: str | None = None,
    ) -> list[dict]:
        params: dict[str, str | int] = {"patient": patient_id, "_count": count}
        if sort:
            params["_sort"] = sort
        url = f"{self._base}/{resource}"
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("FHIR search failed: %s %s — %s", resource, patient_id, e)
            return []
        bundle = r.json() or {}
        return [e.get("resource", {}) for e in bundle.get("entry", [])]


def _safe(v: Any) -> list[dict]:
    """Convert exceptions/None into an empty list. Bounded-error contract."""
    return v if isinstance(v, list) else []


def load_local_bundle(path: str) -> dict[str, list[dict]]:
    """Load a hand-authored test bundle from disk. Used by demo + tests + offline mode.

    Bundle shape on disk: a single FHIR Bundle JSON or our flat shape
    (`{ResourceType: [{...}]}`). We accept both.
    """
    import json
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if "entry" in raw:
        out: dict[str, list[dict]] = {}
        for e in raw.get("entry", []):
            res = e.get("resource", {}) or {}
            t = res.get("resourceType")
            if t:
                out.setdefault(t, []).append(res)
        for k in [
            "MedicationRequest", "MedicationStatement", "AllergyIntolerance",
            "Condition", "Observation", "Encounter", "Task", "Appointment",
        ]:
            out.setdefault(k, [])
        return out
    return raw
