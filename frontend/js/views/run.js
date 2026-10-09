import { api } from "../api.js";
import { json, requirementsTable, timeline } from "../components.js";
import { clear, errorNotice, h } from "../dom.js";
import { IN_FLIGHT, badge, duration, fmtTime, outcomeBadge, statusBadge } from "../format.js";

export async function runView(id) {
  const root = h("div", null);
  let timer = null;
  let disposed = false;

  async function load() {
    clearTimeout(timer);
    try {
      const run = await api.run(id);
      if (disposed) return;
      clear(root);
      root.append(...render(run));
      if (IN_FLIGHT.has(run.status)) timer = setTimeout(load, 2000);
    } catch (error) {
      if (disposed) return;
      clear(root);
      root.append(errorNotice(error, load));
    }
  }
  await load();
  return { node: root, dispose: () => { disposed = true; clearTimeout(timer); } };
}

function render(run) {
  const nodes = [];
  nodes.push(h("div", { class: "breadcrumb" }, h("a", { href: "#/runs" }, "Runs"), " / ", run.run_id));
  nodes.push(
    h(
      "div",
      { class: "page-head" },
      h("div", null, h("h1", null, run.vendor_name || "Run"), h("p", { class: "sub" }, statusBadge(run.status), " ", outcomeBadge(run.outcome))),
      run.vendor_id ? h("a", { class: "btn", href: `#/vendors/${encodeURIComponent(run.vendor_id)}` }, "Vendor details") : null,
    ),
  );

  if (IN_FLIGHT.has(run.status)) {
    nodes.push(h("div", { class: "notice", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), " Run in progress; this page refreshes every 2 seconds."));
  }
  if (run.status === "WAITING_APPROVAL") {
    nodes.push(
      h(
        "div",
        { class: "notice warn" },
        h("strong", null, "Human action required. "),
        "Evidence verified valid; the run is paused until a Compliance Officer decides.",
        run.approval_id ? h("div", { class: "actions" }, h("a", { class: "btn primary", href: `#/approvals/${encodeURIComponent(run.approval_id)}` }, "Open approval request")) : null,
      ),
    );
  } else if (run.status === "FAILED") {
    nodes.push(h("div", { class: "notice error" }, h("strong", null, "This run did not succeed. "), run.summary || ""));
  } else if (run.status === "COMPLETED") {
    nodes.push(h("div", { class: "notice success" }, "Verified complete: evidence valid and a recorded Compliance Officer approval."));
  }

  nodes.push(
    h(
      "section",
      { class: "card" },
      h("h2", null, "Run"),
      h(
        "dl",
        { class: "facts" },
        h("dt", null, "Goal"), h("dd", null, run.goal),
        h("dt", null, "Started"), h("dd", null, fmtTime(run.started_at)),
        h("dt", null, "Completed"), h("dd", null, fmtTime(run.completed_at)),
        h("dt", null, "Duration"), h("dd", null, duration(run.started_at, run.completed_at)),
        h("dt", null, "Run id"), h("dd", { class: "mono" }, run.run_id),
      ),
    ),
  );

  nodes.push(
    h(
      "section",
      { class: "verified-box", style: "margin-bottom:1rem" },
      h("h2", null, badge("info", "Verified"), " Deterministic verification"),
      h("p", { class: "hint" }, "Computed by the policy engine from persisted evidence and approvals. The model has no influence on this result."),
      run.summary ? h("p", null, run.summary) : null,
      requirementsTable(run.requirements),
    ),
  );

  nodes.push(
    h(
      "section",
      { class: "model-box", style: "margin-bottom:1rem" },
      h("h2", null, badge("model", "Unverified"), " Model's statement"),
      run.model_summary ? h("p", null, run.model_summary) : h("p", { class: "hint" }, "The model produced no summary."),
      h("p", { class: "hint" }, "Advisory text only. It never decides the outcome or approval."),
    ),
  );

  nodes.push(
    h(
      "section",
      { class: "card" },
      h("h2", null, `Tool calls (${run.tool_calls.length})`),
      run.tool_calls.length
        ? run.tool_calls.map((c) =>
            h(
              "details",
              null,
              h("summary", null, h("span", { class: "mono" }, c.tool_name), " ", badge(c.status === "SUCCESS" ? "ok" : "bad", c.status), c.error_code ? ` ${c.error_code}` : "", " ", h("span", { class: "hint" }, fmtTime(c.timestamp))),
              h("strong", null, "Input"),
              json(c.input),
              h("strong", null, "Result"),
              json(c.output),
            ),
          )
        : h("p", { class: "empty" }, "The agent made no tool calls."),
    ),
  );

  nodes.push(h("section", { class: "card" }, h("h2", null, "Audit timeline"), h("p", { class: "hint" }, "Persisted events, each tagged by who produced it."), timeline(run.audit_events)));
  return nodes;
}
