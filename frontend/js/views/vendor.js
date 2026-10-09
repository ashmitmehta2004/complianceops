import { api } from "../api.js";
import { documentsTable, requirementsTable } from "../components.js";
import { h } from "../dom.js";
import { decisionBadge, evidenceBadge, fmtTime, outcomeBadge, reviewBadge, statusBadge } from "../format.js";

export async function vendorView(id) {
  const v = await api.vendor(id);

  const reviews = v.reviews.length
    ? v.reviews.map((r) =>
        h(
          "div",
          { class: "card" },
          h("div", null, "Review ", statusBadge(r.status), " ", h("span", { class: "hint" }, `created ${fmtTime(r.created_at)}`)),
          r.approvals.length
            ? h(
                "ul",
                { class: "plain" },
                r.approvals.map((a) =>
                  h(
                    "li",
                    null,
                    decisionBadge(a.decision),
                    " ",
                    h("a", { href: `#/approvals/${encodeURIComponent(a.id)}` }, "approval request"),
                    a.is_fixture ? " (demo fixture, not a real officer decision)" : "",
                    a.decided_by && !a.is_fixture ? ` — ${a.decided_by}` : "",
                  ),
                ),
              )
            : h("p", { class: "hint" }, "No approval requested for this review."),
        ),
      )
    : h("p", { class: "empty" }, "No compliance review yet. Starting a task creates one.");

  const runs = v.runs.length
    ? h(
        "ul",
        { class: "plain" },
        v.runs.map((r) =>
          h("li", null, h("a", { href: `#/runs/${encodeURIComponent(r.run_id)}` }, fmtTime(r.started_at)), " ", statusBadge(r.status), " ", outcomeBadge(r.outcome)),
        ),
      )
    : h("p", { class: "empty" }, "No runs for this vendor.");

  return h(
    "div",
    null,
    h("div", { class: "breadcrumb" }, h("a", { href: "#/" }, "Overview"), " / ", v.name),
    h(
      "div",
      { class: "page-head" },
      h("div", null, h("h1", null, v.name), v.description ? h("p", { class: "sub" }, v.description) : null, h("p", { class: "sub" }, h("span", { class: "hint" }, "Compliance: "), evidenceBadge(v), "  ", h("span", { class: "hint" }, "Review: "), reviewBadge(v.review_status))),
      h("a", { class: "btn primary", href: `#/new?vendor=${encodeURIComponent(v.name)}` }, "Start compliance task"),
    ),
    h("section", { class: "card" }, h("h2", null, "Requirements (live deterministic evaluation)"), requirementsTable(v.requirements)),
    h("section", { class: "card" }, h("h2", null, "Evidence on file"), documentsTable(v.documents), h("p", { class: "hint" }, "Evidence upload is not supported in this prototype yet; documents come from seeded or pre-extracted data.")),
    h("section", null, h("h2", null, "Reviews and approvals"), reviews),
    h("section", { class: "card" }, h("h2", null, "Run history"), runs),
  );
}
