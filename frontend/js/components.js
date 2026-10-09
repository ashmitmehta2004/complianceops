// Reusable presentational pieces built from backend data.

import { h } from "./dom.js";
import { badge, fmtDate, fmtTime, reqBadge } from "./format.js";

export function requirementsTable(requirements) {
  if (!requirements.length) return h("p", { class: "empty" }, "No verification results yet.");
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      null,
      h("thead", null, h("tr", null, h("th", null, "Requirement"), h("th", null, "Status"), h("th", null, "Why"))),
      h(
        "tbody",
        null,
        requirements.map((r) =>
          h(
            "tr",
            null,
            h("td", null, r.label),
            h("td", null, reqBadge(r.status)),
            h(
              "td",
              null,
              r.explanation,
              r.expires_on ? h("div", { class: "hint" }, `Counts as valid until ${fmtDate(r.expires_on)}`) : null,
              h("div", { class: "hint mono" }, r.reason),
            ),
          ),
        ),
      ),
    ),
  );
}

const DOC_LABEL = { soc2_report: "SOC 2 report", dpa: "DPA", security_questionnaire: "Security questionnaire" };

function factsText(doc) {
  return Object.entries(doc.facts)
    .map(([k, v]) => `${k.replaceAll("_", " ")}: ${v}`)
    .join(" · ");
}

export function documentsTable(documents) {
  if (!documents.length) return h("p", { class: "empty" }, "No evidence on file for this vendor.");
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      null,
      h("thead", null, h("tr", null, h("th", null, "Document"), h("th", null, "File"), h("th", null, "Recorded facts"), h("th", null, "Source"))),
      h(
        "tbody",
        null,
        documents.map((d) =>
          h(
            "tr",
            null,
            h("td", null, DOC_LABEL[d.document_type] || d.document_type),
            h("td", { class: "mono" }, d.filename),
            h("td", null, factsText(d) || "—"),
            h("td", null, d.source === "seed" ? badge("neutral", "seed fixture") : d.source),
          ),
        ),
      ),
    ),
  );
}

// ---- audit timeline: who said it matters, so each event is tagged with its source ----

const SOURCES = {
  model: { label: "Model proposal", kind: "model" },
  tool: { label: "Tool result", kind: "neutral" },
  verifier: { label: "Deterministic verifier", kind: "info" },
  human: { label: "Human decision", kind: "ok" },
  runtime: { label: "Runtime", kind: "neutral" },
};

function describe(e) {
  const d = e.details || {};
  switch (e.event_type) {
    case "run_created":
      return ["runtime", `Run created. Goal: ${d.goal}`];
    case "policy_loaded":
      return ["runtime", `Loaded policy ${d.policy}`];
    case "context_loaded":
      return ["runtime", `Resolved vendor and review${d.review_created ? " (new review created)" : ""}`];
    case "target_unresolved":
      return ["runtime", `Could not resolve a single vendor (candidates: ${(d.candidates || []).join(", ") || "none"})`];
    case "llm_response": {
      const tools = d.tools_requested && d.tools_requested.length ? `; requested tools: ${d.tools_requested.join(", ")}` : "";
      return ["model", `Model turn ${d.iteration} ended with “${d.stop_reason}”${tools}`];
    }
    case "llm_retry":
      return ["runtime", `Retrying model call after transient error (attempt ${d.attempt}): ${d.error}`];
    case "llm_error":
      return ["runtime", `Model call failed: ${d.error}`];
    case "tool_call":
      return ["tool", `${d.tool} → ${d.status}${d.error_code ? ` (${d.error_code})` : ""}`];
    case "model_summary":
      return ["model", `Model’s own summary (unverified): ${d.text}`];
    case "verification":
      return ["verifier", `Policy engine verdict: ${d.outcome}. ${d.summary}`];
    case "approval_pending":
      return ["runtime", "Approval request created; run is paused until a human decides"];
    case "approval_decided":
      return ["human", `${e.actor} (${d.role}) ${d.decision}${d.comment ? ` — “${d.comment}”` : ""}`];
    case "approval_decision_refused":
      return ["human", `${e.actor} attempted ${d.attempted}, refused: ${d.reason}`];
    case "status_changed":
      return ["runtime", `Status ${d.from} → ${d.to}${d.outcome ? ` (${d.outcome})` : ""}${d.reason ? ` — ${d.reason}` : ""}`];
    default:
      return ["runtime", e.event_type];
  }
}

export function timeline(events) {
  if (!events.length) return h("p", { class: "empty" }, "No audit events.");
  return h(
    "ol",
    { class: "timeline" },
    events.map((e) => {
      const [source, text] = describe(e);
      const src = SOURCES[source];
      return h(
        "li",
        { class: `src-${source}` },
        h("div", { class: "when" }, fmtTime(e.timestamp), " · ", badge(src.kind, src.label), " ", h("span", { class: "mono" }, e.event_type)),
        h("div", { class: "what" }, text),
      );
    }),
  );
}

export function json(value) {
  return h("pre", null, JSON.stringify(value, null, 2));
}

export function stat(label, value) {
  return h("div", { class: "card stat" }, h("div", { class: "num" }, String(value)), h("div", { class: "label" }, label));
}
