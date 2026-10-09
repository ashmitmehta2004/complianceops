// Labels, badge kinds and formatting. Presentation only: no status is ever decided here.

import { h } from "./dom.js";

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

export function fmtDate(iso) {
  return iso ? String(iso) : "—";
}

export function duration(start, end) {
  if (!start || !end) return "—";
  const ms = new Date(end) - new Date(start);
  if (!(ms >= 0)) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export const OUTCOME_LABEL = {
  COMPLIANT_APPROVED: "Compliant — approved",
  AWAITING_APPROVAL: "Awaiting human approval",
  EVIDENCE_DEFICIENT: "Evidence deficient",
  APPROVAL_REJECTED: "Approval rejected",
  LIMIT_REACHED: "Limit reached",
  MODEL_ERROR: "Model / provider error",
  INVALID_TARGET: "Invalid target",
  INTERRUPTED: "Interrupted",
};

const OUTCOME_KIND = {
  COMPLIANT_APPROVED: "ok",
  AWAITING_APPROVAL: "warn",
  EVIDENCE_DEFICIENT: "bad",
  APPROVAL_REJECTED: "bad",
  LIMIT_REACHED: "bad",
  MODEL_ERROR: "bad",
  INVALID_TARGET: "bad",
  INTERRUPTED: "bad",
};

const STATUS_KIND = {
  COMPLETED: "ok",
  FAILED: "bad",
  WAITING_APPROVAL: "warn",
  RECEIVED: "info",
  PLANNING: "info",
  EXECUTING: "info",
  VERIFYING: "info",
  RETRYING: "info",
};

const REQ_KIND = { VALID: "ok", MISSING: "bad", EXPIRED: "bad", INVALID: "bad", INCOMPLETE: "warn" };
const DECISION_KIND = { PENDING: "warn", APPROVED: "ok", REJECTED: "bad" };

export const IN_FLIGHT = new Set(["RECEIVED", "PLANNING", "EXECUTING", "VERIFYING", "RETRYING"]);

export function badge(kind, text) {
  return h("span", { class: `badge ${kind}` }, text);
}

const title = (s) => s.charAt(0) + s.slice(1).toLowerCase().replaceAll("_", " ");

export const statusBadge = (s) => (s ? badge(STATUS_KIND[s] || "neutral", title(s)) : badge("neutral", "No review"));
export const outcomeBadge = (o) => (o ? badge(OUTCOME_KIND[o] || "neutral", OUTCOME_LABEL[o] || o) : badge("neutral", "—"));
export const reqBadge = (s) => badge(REQ_KIND[s] || "neutral", title(s));
export const decisionBadge = (d) => badge(DECISION_KIND[d] || "neutral", title(d));

// Compliance assessment (what the policy engine says about the evidence). Independent of the
// review lifecycle below: a vendor can have missing evidence and no review at the same time.
export function evidenceBadge(vendor) {
  if (vendor.evidence_compliant) return badge("ok", "Evidence valid");
  const evidence = vendor.requirements.filter((r) => r.requirement !== "COMPLIANCE_OFFICER_APPROVAL");
  if (evidence.length && evidence.every((r) => r.status === "MISSING")) return badge("bad", "Evidence missing");
  return badge("bad", "Evidence deficient");
}

// Review lifecycle (workflow state), not a compliance judgement.
export const reviewBadge = (s) => (s ? statusBadge(s) : badge("neutral", "No review started"));

export function approvalBadge(vendor) {
  if (vendor.approval_satisfied) return badge("ok", "Approved");
  if (vendor.pending_approval_id) return badge("warn", "Approval pending");
  const approval = vendor.requirements.find((r) => r.requirement === "COMPLIANCE_OFFICER_APPROVAL");
  if (approval && approval.reason === "APPROVAL_REJECTED") return badge("bad", "Rejected");
  return badge("neutral", "Not requested");
}
