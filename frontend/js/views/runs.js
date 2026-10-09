import { api } from "../api.js";
import { clear, errorNotice, h } from "../dom.js";
import { IN_FLIGHT, fmtTime, duration, outcomeBadge, statusBadge } from "../format.js";

const STATUSES = ["", "RECEIVED", "PLANNING", "EXECUTING", "VERIFYING", "WAITING_APPROVAL", "COMPLETED", "FAILED"];

export async function runsView() {
  const vendors = await api.vendors();
  const state = { status: "", vendor: "", newestFirst: true };
  const body = h("div", null);
  let timer = null;
  let disposed = false;

  const statusSel = h("select", { id: "f-status", onchange: (e) => { state.status = e.target.value; load(); } }, STATUSES.map((s) => h("option", { value: s }, s || "All statuses")));
  const vendorSel = h("select", { id: "f-vendor", onchange: (e) => { state.vendor = e.target.value; load(); } }, h("option", { value: "" }, "All vendors"), vendors.map((v) => h("option", { value: v.id }, v.name)));
  const sortBtn = h("button", { class: "btn", type: "button", onclick: () => { state.newestFirst = !state.newestFirst; load(); } }, "Sort: newest first");

  async function load() {
    clearTimeout(timer);
    sortBtn.textContent = state.newestFirst ? "Sort: newest first" : "Sort: oldest first";
    try {
      const runs = await api.runs({ status: state.status, vendor_id: state.vendor, limit: 200 });
      if (disposed) return;
      if (!state.newestFirst) runs.reverse();
      clear(body);
      body.append(table(runs));
      // Honest polling: only while something is actually executing.
      if (runs.some((r) => IN_FLIGHT.has(r.status))) timer = setTimeout(load, 3000);
    } catch (error) {
      if (disposed) return;
      clear(body);
      body.append(errorNotice(error, load));
    }
  }

  const node = h(
    "div",
    null,
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Run history"), h("p", { class: "sub" }, "Every agent run, read from the database.")), h("a", { class: "btn primary", href: "#/new" }, "New task")),
    h(
      "div",
      { class: "filters" },
      h("div", null, h("label", { for: "f-status" }, "Status"), statusSel),
      h("div", null, h("label", { for: "f-vendor" }, "Vendor"), vendorSel),
      sortBtn,
      h("button", { class: "btn", type: "button", onclick: load }, "Refresh"),
    ),
    h("div", { class: "card" }, body),
  );
  await load();
  return { node, dispose: () => { disposed = true; clearTimeout(timer); } };
}

function table(runs) {
  if (!runs.length) return h("p", { class: "empty" }, "No runs match.");
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      null,
      h("thead", null, h("tr", null, ["Started", "Vendor", "Status", "Outcome", "Duration", ""].map((t) => h("th", null, t)))),
      h(
        "tbody",
        null,
        runs.map((r) =>
          h(
            "tr",
            null,
            h("td", { class: "nowrap" }, fmtTime(r.started_at)),
            h("td", null, r.vendor_name || "—"),
            h("td", null, statusBadge(r.status)),
            h("td", null, outcomeBadge(r.outcome)),
            h("td", null, duration(r.started_at, r.completed_at)),
            h("td", null, h("a", { href: `#/runs/${encodeURIComponent(r.run_id)}` }, "Inspect")),
          ),
        ),
      ),
    ),
  );
}
