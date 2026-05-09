// TransitionPilot demo UI — split-screen forensic replay with Logic-Link hover.
// Hits the local MCP server at http://127.0.0.1:8089/demo/run.

const API = (window.MCP_BASE || "http://127.0.0.1:8089");

// Optional case narratives — surfaces the "Day 6 already happened" framing
// the council recommended. Keyed by case_id.
const NARRATIVES = {
  ahrq_warfarin_tmp_smx: `67-year-old woman with atrial fibrillation, stable on chronic
    warfarin, admitted Day -5 for cellulitis. Discharged today on Bactrim DS.
    On Day 6 she returns to the ED bleeding — INR 8.2. The drug-drug interaction
    was visible at discharge in the FHIR record. Nobody saw it.`,
  case_2_duplicate_opioid: `Post-op orthopedic patient discharged on TWO simultaneous
    opioids — oxycodone PRN and IV-equivalent hydromorphone tabs. On Day 3 the
    patient is found altered at home; respiratory depression from compounded dosing.`,
  case_3_insulin_no_glucose: `Newly insulin-dependent diabetic discharged after a DKA
    admission with insulin glargine 20 units qhs — but no glucose-monitoring
    follow-up task. Day 4 she is hypoglycemic at 32 mg/dL alone in her apartment.`,
  case_4_hf_no_followup: `74yo HFrEF patient, acute-on-chronic decompensation, discharged
    today. No 7-day follow-up appointment. CMS readmission penalty trigger. Day 9
    she is back, fluid-overloaded.`,
  case_5_allergy_conflict: `40yo woman with documented anaphylactic penicillin allergy
    is discharged on amoxicillin (a penicillin-class antibiotic). The prescriber
    didn't see the allergy banner. Day 1 — anaphylaxis at home.`,
};

const elCases = document.getElementById("case-select");
const elRun = document.getElementById("run");
const elMemo = document.getElementById("memo");
const elNarr = document.getElementById("case-narrative");
const elLatency = document.getElementById("latency");
const elRuntime = document.getElementById("runtime");
const elEv = document.getElementById("evidence-panel");
const elEvBody = document.getElementById("evidence-body");

let lastBundle = null;   // for evidence panel lookup

// ── Init ────────────────────────────────────────────────────
async function init() {
  try {
    const r = await fetch(`${API}/demo/cases`);
    const j = await r.json();
    elCases.innerHTML = j.cases.map(c =>
      `<option value="${c}">${prettyName(c)}</option>`
    ).join("");
    updateNarrative();
  } catch (e) {
    elCases.innerHTML = `<option>(server offline — start: python -m uvicorn transition_pilot.server:app --port 8089)</option>`;
  }
  elCases.addEventListener("change", updateNarrative);
  elRun.addEventListener("click", run);
  document.getElementById("close-evidence").addEventListener("click", () => {
    elEv.hidden = true;
  });
}
init();

function prettyName(c) {
  return c.replace(/_/g, " ").replace(/\b\w/g, ch => ch.toUpperCase());
}
function updateNarrative() {
  const id = elCases.value;
  elNarr.textContent = NARRATIVES[id] || "Synthetic case bundle loaded from cases/.";
}

// ── Run ─────────────────────────────────────────────────────
async function run() {
  elMemo.innerHTML = "<div class='memo-empty'>Calling MCP server…</div>";
  elLatency.textContent = "";
  const t0 = performance.now();
  try {
    const r = await fetch(`${API}/demo/run`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({case_id: elCases.value}),
    });
    const j = await r.json();
    const ms = Math.round(performance.now() - t0);
    elLatency.textContent = `${ms} ms · ${(j.timing && j.timing.total_ms) || 0}ms server`;
    elRuntime.textContent = (j.timing && j.timing.total_ms) || ms;

    // Cache the original bundle for evidence-panel lookups.
    lastBundle = await loadCaseBundle(elCases.value);
    renderMemo(j.memo, j);
  } catch (e) {
    elMemo.innerHTML = `<div class='memo-empty'>Error: ${e.message}</div>`;
  }
}

async function loadCaseBundle(caseId) {
  // Pull the raw case file from the server's bundle endpoint — used by the
  // evidence panel to render the FHIR JSON for any clicked Logic-Link.
  try {
    const r = await fetch(`${API}/demo/cases/${caseId}.json`);
    if (r.ok) return await r.json();
  } catch (_) {}
  return null;
}

// ── Render ──────────────────────────────────────────────────
function renderMemo(memo, full) {
  if (!memo) { elMemo.textContent = "(empty)"; return; }
  let html = "";

  // Failures prevented
  html += "<div class='failures-prevented'>";
  if (memo.failures_prevented && memo.failures_prevented.length) {
    for (const f of memo.failures_prevented) {
      html += `
        <div class='failure-card ${f.severity}'>
          <div class='failure-head'>
            <div class='failure-title'>${escapeHtml(f.title)}</div>
            <span class='severity-badge ${f.severity}'>${f.severity}</span>
            <span class='exposure-badge ${f.exposure_band}'>exposure: ${f.exposure_band}</span>
          </div>
          <div class='failure-summary'>${escapeHtml(f.summary)}</div>
          <div class='owner-action'>
            <span class='lbl'>Owner</span><span>${escapeHtml(f.suggested_owner)}</span>
            <span class='lbl'>Action</span><span>${escapeHtml(f.suggested_action)}</span>
          </div>
          <div class='logic-links'>
            ${(f.logic_link || []).map(l => renderLink(l)).join("")}
          </div>
        </div>`;
    }
  } else {
    html += "<div class='memo-empty'>No high-risk patterns triggered.</div>";
  }
  html += "</div>";

  // Tasks
  if (memo.audit_tasks && memo.audit_tasks.length) {
    html += "<div class='tasks'><h3>Audit tasks (FHIR-Task export ready)</h3>";
    for (const t of memo.audit_tasks) {
      html += `
        <div class='task-row'>
          <span class='task-owner-pill'>${escapeHtml(t.owner)} · ${t.due_within_hours}h</span>
          <div>
            <div class='task-title'>${escapeHtml(t.title)}</div>
            <div class='task-rationale'>${escapeHtml(t.rationale)}</div>
          </div>
        </div>`;
    }
    html += "</div>";
  }

  // Med table — only render if synthesis filled it (otherwise stub mode)
  if (memo.medication_changes && memo.medication_changes.length) {
    html += "<div class='med-table'><h3>Discharge medications</h3>";
    for (const m of memo.medication_changes) {
      html += `
        <div class='med-row'>
          <span class='med-action ${m.action}'>${m.action}</span>
          <div>
            <div class='med-name'>${escapeHtml(m.name)}</div>
            <div class='med-reason'>${escapeHtml(m.reason)}</div>
          </div>
          <div class='logic-links'>
            ${(m.logic_link || []).map(l => renderLink(l)).join("")}
          </div>
        </div>`;
    }
    html += "</div>";
  }

  if (full && full.used_evidence_or_null_fallback) {
    html += `<div class='memo-empty' style='margin-top:12px'>
      ⚠ Confidence: needs review (some citations dropped or LLM offline). Notes: ${
        (full.notes || []).join(" / ")
      }</div>`;
  }

  elMemo.innerHTML = html;

  // Wire up Logic-Link hover/click
  elMemo.querySelectorAll(".logic-link").forEach(el => {
    el.addEventListener("click", () => showEvidence(
      el.dataset.resourceType, el.dataset.resourceId, el.dataset.note,
    ));
  });
}

function renderLink(l) {
  return `<span class='logic-link'
    data-resource-type='${escapeAttr(l.resource_type)}'
    data-resource-id='${escapeAttr(l.resource_id)}'
    data-note='${escapeAttr(l.note || "")}'>
    ${escapeHtml(l.resource_type)}/${escapeHtml(l.resource_id)}
  </span>`;
}

function showEvidence(resType, resId, note) {
  let body = `<div class='evidence-row'>
      <div class='ref'>${escapeHtml(resType)}/${escapeHtml(resId)}</div>
      <div class='note'>${escapeHtml(note || "—")}</div>`;

  // Try to find the actual FHIR resource in the cached bundle
  if (lastBundle && resId !== "—") {
    const entries = (lastBundle.entry || []);
    const hit = entries.find(e =>
      (e.resource || {}).resourceType === resType
      && (e.resource || {}).id === resId
    );
    if (hit) {
      body += `<pre>${escapeHtml(JSON.stringify(hit.resource, null, 2))}</pre>`;
    } else {
      body += `<div class='note muted'>No matching resource in bundle (sentinel ID).</div>`;
    }
  }
  body += "</div>";
  elEvBody.innerHTML = body;
  elEv.hidden = false;
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function escapeAttr(s) { return escapeHtml(s).replace(/"/g, "&quot;"); }
