// Thin client for the ComplianceOps backend. Every failure becomes an ApiError with a message
// that is safe to show; nothing is swallowed here.

export class ApiError extends Error {
  constructor(status, code, message, errors) {
    super(message);
    this.name = "ApiError";
    this.status = status; // 0 = backend unreachable
    this.code = code || null;
    this.errors = errors || [];
  }
}

async function request(method, path, body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the backend. Check that the server is running.");
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : response.statusText || "Request failed";
    throw new ApiError(response.status, data && data.code, detail, data && data.errors);
  }
  return data;
}

function query(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") q.set(k, v);
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

const enc = encodeURIComponent;

export const api = {
  meta: () => request("GET", "/meta"),
  ready: () => request("GET", "/ready"),
  vendors: () => request("GET", "/vendors"),
  vendor: (id) => request("GET", `/vendors/${enc(id)}`),
  createVendor: (name, description) => request("POST", "/vendors", description ? { name, description } : { name }),
  runs: (params) => request("GET", `/runs${query(params)}`),
  run: (id) => request("GET", `/runs/${enc(id)}`),
  startRun: (vendorName) => request("POST", "/runs", { vendor_name: vendorName }),
  approvals: (status) => request("GET", `/approvals${query({ status })}`),
  approval: (id) => request("GET", `/approvals/${enc(id)}`),
  decide: (id, decision, comment) =>
    request("POST", `/approvals/${enc(id)}/decision`, comment ? { decision, comment } : { decision }),
};
