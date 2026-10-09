import { api } from "../api.js";
import { documentsTable, requirementsTable } from "../components.js";
import { clear, errorNotice, h } from "../dom.js";
import { badge, decisionBadge, evidenceBadge, fmtTime, outcomeBadge } from "../format.js";
import { refreshPending } from "../main.js";

export async function approvalsView() {
  const body = h("div", null);
  let tab = "PENDING";

  async function load() {
    clear(body);
    try {
      const list = await api.approvals(tab === "PENDING" ? "PENDING" : undefined);
      const shown = tab === "PENDING" ? list : list.filter((a) => a.decision !== "PENDING");
      body.append(
        shown.length
          ? h(
              "div",
              { class: "table-wrap" },
              h(
                "table",
                null,
                h("thead", null, h("tr", null, ["Vendor", "Decision", "Requested", "Decided by", "Evidence", ""].map((t) => h("th", null, t)))),
                h(
                  "tbody",
                  null,
                  shown.map((a) =>
                    h(
                      "tr",
                      null,
                      h("td", null, a.vendor_name),
                      h("td", null, decisionBadge(a.decision), a.is_fixture ? [" ", badge("neutral", "demo fixture")] : null),
                      h("td", null, fmtTime(a.requested_at)),
                      h("td", null, a.decided_by || "—"),
                      h("td", null, evidenceBadge(a)),
                      h("td", null, h("a", { href: `#/approvals/${encodeURIComponent(a.id)}` }, a.decision === "PENDING" ? "Review" : "View")),
                    ),
                  ),
                ),
              ),
            )
          : h("p", { class: "empty" }, tab === "PENDING" ? "No approvals are waiting for a decision." : "No decided approvals."),
      );
    } catch (error) {
      body.append(errorNotice(error, load));
    }
  }

  const mkTab = (key, label) =>
    h("button", { type: "button", role: "tab", "aria-selected": String(tab === key), onclick: () => { tab = key; for (const b of tabs.children) b.setAttribute("aria-selected", String(b.dataset.key === tab)); load(); }, "data-key": key }, label);
  const tabs = h("div", { class: "tabs", role: "tablist" }, mkTab("PENDING", "Pending"), mkTab("DECIDED", "Decided"));

  await load();
  return h("div", null, h("h1", null, "Approval queue"), h("p", { class: "sub" }, "Decisions here are made by a human and recorded in the database."), tabs, h("div", { class: "card" }, body));
}

export async function approvalView(id) {
  const root = h("div", null);

  async function load(flash) {
    clear(root);
    let a;
    try {
      a = await api.approval(id);
    } catch (error) {
      root.append(errorNotice(error, () => load()));
      return;
    }
    if (flash) root.append(flash);
    root.append(...render(a));
  }

  function render(a) {
    const pending = a.decision === "PENDING";
    const nodes = [
      h("div", { class: "breadcrumb" }, h("a", { href: "#/approvals" }, "Approvals"), " / ", a.vendor_name),
      h("div", { class: "page-head" }, h("div", null, h("h1", null, `Approval for ${a.vendor_name}`), h("p", { class: "sub" }, decisionBadge(a.decision), " ", evidenceBadge(a)))),
    ];
    if (a.is_fixture) nodes.push(h("div", { class: "notice warn" }, "This approval is seeded demo fixture data, not a real Compliance Officer decision."));

    nodes.push(
      h(
        "section",
        { class: "card" },
        h("h2", null, "Request"),
        h(
          "dl",
          { class: "facts" },
          h("dt", null, "Vendor"), h("dd", null, h("a", { href: `#/vendors/${encodeURIComponent(a.vendor_id)}` }, a.vendor_name)),
          h("dt", null, "Requested"), h("dd", null, fmtTime(a.requested_at)),
          h("dt", null, "Review id"), h("dd", { class: "mono" }, a.review_id),
          h("dt", null, "Runs waiting"), h("dd", null, a.run_ids.length ? a.run_ids.map((r) => [h("a", { href: `#/runs/${encodeURIComponent(r)}` }, r.slice(0, 8)), " "]) : "none"),
          a.resolved_at ? [h("dt", null, "Decided"), h("dd", null, fmtTime(a.resolved_at))] : null,
          a.decided_by ? [h("dt", null, "Decided by"), h("dd", null, `${a.decided_by} (role: ${a.role})`)] : null,
          a.comment ? [h("dt", null, "Comment"), h("dd", null, a.comment)] : null,
        ),
      ),
      h("section", { class: "verified-box", style: "margin-bottom:1rem" }, h("h2", null, badge("info", "Verified"), " Current verification"), requirementsTable(a.requirements)),
      h("section", { class: "card" }, h("h2", null, "Evidence"), documentsTable(a.documents)),
    );

    if (pending) nodes.push(decisionPanel(a));
    else nodes.push(h("div", { class: "notice" }, `This request was ${a.decision.toLowerCase()}. Decisions are final.`));
    return nodes;
  }

  function decisionPanel(a) {
    const panel = h("section", { class: "card" }, h("h2", null, "Decision"));
    if (!a.can_approve) {
      panel.append(h("div", { class: "notice error", role: "alert" }, a.blocked_reason || "Approval is currently blocked."));
    }
    const comment = h("textarea", { id: "comment", maxlength: "1000", "aria-describedby": "comment-hint" });
    const feedback = h("div", { "aria-live": "polite" });
    const buttons = h("div", { class: "actions" });
    panel.append(h("div", { class: "field" }, h("label", { for: "comment" }, "Comment (optional)"), comment, h("div", { id: "comment-hint", class: "hint" }, "Recorded with the decision.")), feedback, buttons);

    function resetButtons() {
      clear(buttons);
      buttons.append(
        h("button", { class: "btn primary", type: "button", disabled: !a.can_approve, onclick: () => confirm("APPROVED") }, "Approve"),
        h("button", { class: "btn danger", type: "button", onclick: () => confirm("REJECTED") }, "Reject"),
      );
    }

    function confirm(decision) {
      clear(buttons);
      buttons.append(
        h("span", null, `Confirm: ${decision === "APPROVED" ? "approve" : "reject"} ${a.vendor_name}? This is final.`),
        h("button", { class: "btn " + (decision === "APPROVED" ? "primary" : "danger"), type: "button", onclick: () => submit(decision) }, `Yes, ${decision === "APPROVED" ? "approve" : "reject"}`),
        h("button", { class: "btn", type: "button", onclick: resetButtons }, "Cancel"),
      );
    }

    async function submit(decision) {
      for (const b of buttons.querySelectorAll("button")) b.disabled = true;
      clear(feedback);
      try {
        const result = await api.decide(a.id, decision, comment.value.trim());
        refreshPending();
        const finals = result.resumed_runs.map((r) => `${r.vendor_name}: ${r.status}`).join("; ");
        await load(h("div", { class: "notice success", role: "status" }, `Decision recorded: ${result.approval.decision.toLowerCase()}.`, finals ? ` Resumed run → ${finals}.` : "", " ", result.resumed_runs.map((r) => [h("a", { href: `#/runs/${encodeURIComponent(r.run_id)}` }, "Open run"), " ", outcomeBadge(r.outcome), " "])));
      } catch (error) {
        // Stale/conflicting/blocked decisions: show the backend's reason, then reload truth.
        await load(h("div", { class: "notice error", role: "alert" }, `Decision not recorded: ${error.message}`));
        refreshPending();
      }
    }

    resetButtons();
    return panel;
  }

  await load();
  return root;
}
