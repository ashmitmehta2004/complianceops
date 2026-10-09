import { api } from "../api.js";
import { clear, h } from "../dom.js";

const NAME_MAX = 200;
const DESCRIPTION_MAX = 500;

export function newVendorView() {
  const name = h("input", { id: "name", type: "text", required: true, maxlength: NAME_MAX, autocomplete: "off", "aria-describedby": "name-err" });
  const nameError = h("div", { id: "name-err", class: "field-error", role: "alert" });
  const description = h("textarea", { id: "description", maxlength: DESCRIPTION_MAX, "aria-describedby": "description-err" });
  const descriptionError = h("div", { id: "description-err", class: "field-error", role: "alert" });
  const feedback = h("div", { "aria-live": "polite" });
  const submit = h("button", { class: "btn primary", type: "submit" }, "Add vendor");

  const form = h(
    "form",
    { novalidate: true },
    h("div", { class: "field" }, h("label", { for: "name" }, "Vendor name"), name, nameError),
    h("div", { class: "field" }, h("label", { for: "description" }, "Description (optional)"), description, descriptionError),
    h("p", { class: "hint" }, "Creating a vendor records no evidence and requests no approval. Until documents are on file, every requirement is reported as missing."),
    submit,
  );

  const fieldFor = { name: nameError, description: descriptionError };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clear(feedback);
    nameError.textContent = "";
    descriptionError.textContent = "";
    const value = name.value.trim();
    if (!value) {
      nameError.textContent = "Enter a vendor name.";
      name.focus();
      return;
    }
    if (description.value.trim().length > DESCRIPTION_MAX) {
      descriptionError.textContent = `Keep the description under ${DESCRIPTION_MAX} characters.`;
      description.focus();
      return;
    }
    submit.disabled = true;
    try {
      const vendor = await api.createVendor(value, description.value.trim() || undefined);
      form.reset();
      feedback.append(
        h(
          "div",
          { class: "notice success", role: "status" },
          `Vendor “${vendor.name}” was added. It has no evidence on file, so it is evidence-deficient until documents are recorded.`,
          h(
            "div",
            { class: "actions" },
            h("a", { class: "btn small", href: `#/vendors/${encodeURIComponent(vendor.id)}` }, "Open vendor"),
            " ",
            h("a", { class: "btn small primary", href: `#/new?vendor=${encodeURIComponent(vendor.name)}` }, "Start compliance review"),
          ),
        ),
      );
    } catch (error) {
      if (error.status === 422 && error.errors.length) {
        for (const e of error.errors) (fieldFor[e.field] || nameError).textContent = e.message;
      } else if (error.status === 409) {
        nameError.textContent = "A vendor with this name already exists.";
        name.focus();
      } else {
        feedback.append(h("div", { class: "notice error", role: "alert" }, `Could not add the vendor: ${error.message}`));
      }
    } finally {
      submit.disabled = false;
    }
  });

  return h(
    "div",
    null,
    h("div", { class: "breadcrumb" }, h("a", { href: "#/" }, "Overview"), " / Add vendor"),
    h("h1", null, "Add vendor"),
    h("p", { class: "sub" }, "Register a vendor so it can be reviewed."),
    h("div", { class: "card" }, form, feedback),
  );
}
