import { api } from "../api.js";
import { stat } from "../components.js";
import { h } from "../dom.js";
import { approvalBadge, evidenceBadge, fmtTime, outcomeBadge, reviewBadge, statusBadge } from "../format.js";

export async function overviewView() {
  const [vendors, pending, runs] = await Promise.all([api.vendors(), api.approvals("PENDING"), api.runs({ limit: 8 })]);
  const deficient = vendors.filter((v) => !v.evidence_compliant).length;

  const vendorTable = vendors.length
    ? h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          null,
          h("thead", null, h("tr", null, ["Vendor", "Review lifecycle", "Compliance evidence", "Approval", "Last run", ""].map((t) => h("th", null, t)))),
          h(
            "tbody",
            null,
            vendors.map((v) =>
              h(
                "tr",
                null,
                h("td", null, h("a", { href: `#/vendors/${encodeURIComponent(v.id)}` }, v.name)),
                h("td", null, reviewBadge(v.review_status)),
                h("td", null, evidenceBadge(v)),
                h("td", null, approvalBadge(v)),
                h(
                  "td",
                  null,
                  v.last_run ? [outcomeBadge(v.last_run.outcome), " ", h("span", { class: "hint" }, fmtTime(v.last_run.started_at))] : h("span", { class: "hint" }, "never run"),
                ),
                h("td", { class: "nowrap" }, h("a", { class: "btn small", href: `#/new?vendor=${encodeURIComponent(v.name)}` }, "Start task")),
              ),
            ),
          ),
        ),
      )
    : h("p", { class: "empty" }, "No vendors yet. Add one, or seed demo data with `python -m app.seed`.");

  const pendingList = pending.length
    ? h(
        "ul",
        { class: "plain" },
        pending.map((a) =>
          h("li", null, h("a", { href: `#/approvals/${encodeURIComponent(a.id)}` }, a.vendor_name), " ", h("span", { class: "hint" }, `requested ${fmtTime(a.requested_at)}`)),
        ),
      )
    : h("p", { class: "empty" }, "Nothing is waiting for a human decision.");

  const runList = runs.length
    ? h(
        "ul",
        { class: "plain" },
        runs.map((r) =>
          h("li", null, h("a", { href: `#/runs/${encodeURIComponent(r.run_id)}` }, r.vendor_name || "Unresolved vendor"), " ", statusBadge(r.status), " ", h("span", { class: "hint" }, fmtTime(r.started_at))),
        ),
      )
    : h("p", { class: "empty" }, "No agent runs yet.");

  return h(
    "div",
    null,
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Operations overview"), h("p", { class: "sub" }, "Live vendor compliance state from the backend.")),
      h("a", { class: "btn primary", href: "#/vendors/new" }, "Add vendor"),
    ),
    h(
      "div",
      { class: "grid cols-4" },
      stat("Vendors", vendors.length),
      stat("Pending approvals", pending.length),
      stat("Evidence-deficient vendors", deficient),
      stat("Recent runs", runs.length),
    ),
    h("section", { class: "card" }, h("h2", null, "Vendors"), vendorTable),
    h(
      "div",
      { class: "grid cols-2" },
      h("section", { class: "card" }, h("h2", null, "Pending approvals"), pendingList),
      h("section", { class: "card" }, h("h2", null, "Recent agent activity"), runList, h("a", { href: "#/runs" }, "All runs →")),
    ),
  );
}
