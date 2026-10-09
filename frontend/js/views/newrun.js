import { api } from "../api.js";
import { clear, h } from "../dom.js";

export async function newRunView(params) {
  const vendors = await api.vendors();
  const preselect = params.get("vendor") || "";

  const select = h(
    "select",
    { id: "vendor", required: true, "aria-describedby": "vendor-err" },
    h("option", { value: "" }, "Select a vendor…"),
    vendors.map((v) => h("option", { value: v.name, selected: v.name === preselect }, v.name)),
  );
  const fieldError = h("div", { id: "vendor-err", class: "field-error", role: "alert" });
  const feedback = h("div", { "aria-live": "polite" });
  const submit = h("button", { class: "btn primary", type: "submit" }, "Start compliance task");

  const form = h(
    "form",
    { novalidate: true },
    h("div", { class: "field" }, h("label", { for: "vendor" }, "Vendor"), select, fieldError),
    h("p", { class: "hint" }, "The agent investigates the vendor's evidence with read-only tools. The system, not the agent, verifies the outcome. This request blocks until the run finishes (typically several seconds)."),
    submit,
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clear(feedback);
    fieldError.textContent = "";
    if (!select.value) {
      fieldError.textContent = "Choose a vendor.";
      select.focus();
      return;
    }
    submit.disabled = true;
    select.disabled = true;
    feedback.append(h("div", { class: "loading", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), `Agent is working on ${select.value}…`));
    try {
      const run = await api.startRun(select.value);
      location.hash = `#/runs/${encodeURIComponent(run.run_id)}`;
    } catch (error) {
      clear(feedback);
      const hint =
        error.status === 409
          ? " Open the existing run from the Runs page."
          : error.status === 503
            ? " The server's model provider is not configured."
            : "";
      feedback.append(h("div", { class: "notice error", role: "alert" }, `Could not start the run: ${error.message}.${hint}`));
      submit.disabled = false;
      select.disabled = false;
    }
  });

  return h("div", null, h("h1", null, "New compliance task"), h("p", { class: "sub" }, "Start an autonomous vendor review."), h("div", { class: "card" }, form, feedback));
}
