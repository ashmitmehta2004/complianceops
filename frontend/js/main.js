// Hash router + shell. Each view is `async (ctx) => Node | {node, dispose}`; a stale render
// (the user navigated away while loading) is discarded.

import { api } from "./api.js";
import { clear, errorNotice, h, loading } from "./dom.js";
import { overviewView } from "./views/overview.js";
import { newVendorView } from "./views/newvendor.js";
import { vendorView } from "./views/vendor.js";
import { newRunView } from "./views/newrun.js";
import { runsView } from "./views/runs.js";
import { runView } from "./views/run.js";
import { approvalsView, approvalView } from "./views/approvals.js";

const main = document.getElementById("main");
let current = null; // { token, dispose }
let token = 0;

const routes = [
  [/^#\/?$/, "overview", () => overviewView()],
  [/^#\/vendors\/new$/, "overview", () => newVendorView()],
  [/^#\/vendors\/([^/]+)$/, "overview", (m) => vendorView(decodeURIComponent(m[1]))],
  [/^#\/new(?:\?(.*))?$/, "new", (m) => newRunView(new URLSearchParams(m[1] || ""))],
  [/^#\/runs$/, "runs", () => runsView()],
  [/^#\/runs\/([^/]+)$/, "runs", (m) => runView(decodeURIComponent(m[1]))],
  [/^#\/approvals$/, "approvals", () => approvalsView()],
  [/^#\/approvals\/([^/]+)$/, "approvals", (m) => approvalView(decodeURIComponent(m[1]))],
];

async function render() {
  const mine = ++token;
  if (current && current.dispose) current.dispose();
  current = null;
  const hash = location.hash || "#/";
  let match = null;
  let navKey = "";
  let factory = null;
  for (const [re, key, make] of routes) {
    const m = re.exec(hash);
    if (m) {
      match = m;
      navKey = key;
      factory = make;
      break;
    }
  }
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.dataset.route === navKey) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  clear(main);
  if (!factory) {
    main.append(h("div", { class: "notice error", role: "alert" }, "Page not found. ", h("a", { href: "#/" }, "Go to overview")));
    return;
  }
  main.append(loading());
  try {
    const result = await factory(match);
    if (mine !== token) {
      if (result && result.dispose) result.dispose();
      return;
    }
    const node = result instanceof Node ? result : result.node;
    clear(main);
    main.append(node);
    current = { dispose: result instanceof Node ? null : result.dispose };
    main.focus({ preventScroll: true });
  } catch (error) {
    if (mine !== token) return;
    clear(main);
    main.append(errorNotice(error, render));
  }
  refreshPending();
}

export async function refreshPending() {
  const badge = document.getElementById("nav-pending");
  try {
    const pending = await api.approvals("PENDING");
    badge.textContent = String(pending.length);
    badge.hidden = pending.length === 0;
  } catch {
    badge.hidden = true; // the page itself reports API failures; the badge just stays quiet
  }
}

async function loadEnv() {
  const env = document.getElementById("env");
  try {
    const [meta, ready] = await Promise.all([api.meta(), api.ready()]);
    clear(env);
    env.append(
      `Model: ${meta.llm_provider}`,
      ready.llm_configured ? "" : h("span", { class: "warn" }, " (not configured)"),
      h("br"),
      `Demo actor: ${meta.demo_actor} — not authentication`,
    );
  } catch (error) {
    clear(env);
    env.append(h("span", { class: "warn" }, `Backend unavailable: ${error.message}`));
  }
}

window.addEventListener("hashchange", render);
loadEnv();
render();
