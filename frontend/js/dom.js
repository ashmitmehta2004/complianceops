// Tiny DOM helper. Text is always set via text nodes, never innerHTML, so backend data (which
// includes model output and document contents) cannot inject markup.

export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "disabled" || key === "hidden" || key === "selected" || key === "required") node[key] = Boolean(value);
    else if (key === "value") node.value = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function loading(message = "Loading…") {
  return h("div", { class: "loading", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), message);
}

export function errorNotice(error, onRetry) {
  const box = h("div", { class: "notice error", role: "alert" }, h("strong", null, "Something went wrong. "), error.message || String(error));
  if (onRetry) box.append(h("div", { class: "actions" }, h("button", { class: "btn small", type: "button", onclick: onRetry }, "Retry")));
  return box;
}
