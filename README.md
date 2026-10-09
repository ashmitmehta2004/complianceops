# ComplianceOps

A narrow autonomous AI-agent prototype for vendor compliance review. Given a vendor, an
LLM-driven agent investigates the vendor's evidence through a fixed set of tools, and
deterministic code decides whether the review may be finalized.

## How it works

```
POST /runs {"vendor_name": "Acme Corp"}
   -> AgentRuntime creates an AgentRun, resolves vendor + review + policy itself
   -> loop: model requests tools -> ToolRuntime (lookup, permission, schema validation)
            -> result returned to the model   (10 iterations / 20 tool calls, bounded retries)
   -> deterministic verification: the policy engine re-evaluates persisted data
   -> WAITING_APPROVAL | COMPLETED | FAILED   (outcome says why)

POST /approvals/{id}/decision {"decision": "APPROVED"}      <- a human, not a tool
   -> one transaction: still PENDING? evidence still valid? record decision + actor + time
   -> policy engine re-verifies; waiting runs are finalized from that verdict
```

Lifecycle: Goal -> context (vendor, review, policy) -> tool loop -> verify -> COMPLETED, FAILED
(with `outcome`: `EVIDENCE_DEFICIENT`, `APPROVAL_REJECTED`, `MODEL_ERROR`, `LIMIT_REACHED`,
`INVALID_TARGET`, `INTERRUPTED`) or WAITING_APPROVAL. Resume after a decision is deterministic
re-verification; the model is not called again because it cannot approve and never decides
the outcome.

**Model vs. deterministic code.** The model chooses tools and writes advisory text
(`model_summary`, shown in the UI as *unverified*). Requirement statuses, the outcome, approval
validity and completion come only from the policy engine over persisted rows.

### Safety properties

- **No direct DB access for the model.** It sees tool schemas and tool results only. Every call
  goes through `ToolRuntime` (unknown tool, permission, and argument checks all fail closed).
- **Model output is untrusted.** The model's text is returned as `model_summary` for information
  only. It never changes the outcome.
- **Only verification can finalize.** `COMPLETED` requires the deterministic evaluator to find
  valid SOC 2 Type II (period ended < 12 months ago), a signed DPA, a fully answered security
  questionnaire, **and** a recorded approval by a Compliance Officer.
- **The agent cannot approve.** It has read-only tools. When evidence is valid but no approval
  exists, the runtime creates a `PENDING` approval request and stops in `WAITING_APPROVAL`.
  A human decision must be recorded outside the agent.
- **Everything is auditable.** Status transitions, LLM responses/errors, tool calls (including
  failed ones, stored as `FAILED`) and the verification result are persisted.
- Errors returned over HTTP are generic; exception text and secrets are never included.

### Layout

```
backend/app/
  domain/        enums, policy engine (pure, deterministic)
  persistence/   SQLAlchemy models, SQLite engine/session helpers
  tools/         tool definitions, registry, permissions, ToolRuntime, read-only tools
  agent/         llm.py (protocol + Anthropic), gemini.py (Gemini), providers.py (selection),
                 runtime.py (loop + verification)
  api/           routers (runs, vendors, approvals, system), schemas, views, error shape
  approvals.py   human decision + resume (not a tool)
  agent/workflow.py  state machine, verdict, interrupted-run recovery
  persistence/migrations.py  additive, idempotent schema upgrades
  main.py        create_app() wiring
  seed.py        development seed data
backend/tests/   pytest suite (no network, no API key)
frontend/        build-less ES-module UI served by the backend at /app
docs/            DESIGN_NOTES.md (architecture decisions)
```

## Setup (Python 3.12)

```bash
cd backend
py -3.12 -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"
cp ../.env.example .env           # then edit .env
```

### Environment variables

The agent needs one LLM provider, chosen with `LLM_PROVIDER` (default `anthropic`). There is no
fallback between providers and no mocked mode outside the tests: a misconfigured provider makes
`POST /runs` return `503` (the reason, which names variables only, is logged server-side).

| Variable | When | Purpose |
|---|---|---|
| `LLM_PROVIDER` | optional | `anthropic` (default) or `gemini` |
| `GEMINI_API_KEY` | `gemini` | Google AI Studio key |
| `GEMINI_MODEL` | optional | Defaults to `gemini-3.8-flash`; any Gemini model with function calling |
| `ANTHROPIC_API_KEY` | `anthropic` | Anthropic API key |
| `ANTHROPIC_MODEL` | `anthropic` | Anthropic model id; no default |
| `DEMO_ACTOR` | optional | Identity recorded on approval decisions (default `demo.compliance.officer`); not authentication |
| `DATABASE_URL` | optional | Defaults to `sqlite:///./complianceops.db` (in the working directory) |

Put keys in `backend/.env` (copy from `.env.example`). `.env` is git-ignored; never commit keys,
paste them into tests, or put them in prompts. The API never returns keys or provider error text.

**Gemini** (`LLM_PROVIDER=gemini`): uses the official `google-genai` SDK. Model ids change;
check <https://ai.google.dev/gemini-api/docs/models> and set `GEMINI_MODEL` if the default is
unavailable to your project. Free-tier keys have rate limits and daily quotas and models can be
restricted per project; a quota or access failure surfaces as a `MODEL_ERROR` run
(`ClientError (HTTP 429)` / `(HTTP 404)` in the audit trail), never as a fallback to another
model. Billing and data-use terms depend on your Google plan; check them before sending real
vendor documents.

**Anthropic** (`LLM_PROVIDER=anthropic`): set `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`.

### One real request (smoke test)

Costs one API call; prints the provider, stop reason and requested tool call, never the key:

```bash
cd backend
python -m app.llm_smoke
```

Tests never make network calls: the whole suite runs against scripted fake provider clients.
Passing tests therefore show the adapters and runtime behave correctly against the documented
SDK shapes, not that your key, quota or model works; only the smoke test (or a real run) shows that.

### Database and seed data

Tables are created automatically on server start and by the seed command (`create_all`), then
`persistence/migrations.py` adds any missing columns/indexes to an existing database (additive and
idempotent, so old `complianceops.db` files keep their data). Seed data (repeatable; re-running creates no duplicates):

```bash
python -m app.seed
```

| Vendor | Evidence | Expected run result |
|---|---|---|
| Acme Corp | all valid, no approval | `WAITING_APPROVAL` (pending approval created) |
| Globex Industries | SOC 2 expired, DPA unsigned, questionnaire 30/42 | `FAILED` / `EVIDENCE_DEFICIENT` |
| Initech | nothing on file | `FAILED` / `EVIDENCE_DEFICIENT` |
| Hooli | all valid + seeded Compliance Officer approval | `COMPLETED` |

SOC 2 dates are relative to the day you seed, so re-run the seed command to refresh them.
Hooli's approval is fixture data standing in for a human decision.

## Run

```bash
cd backend
python -m app.seed
uvicorn app.main:app --env-file .env
```

Open <http://127.0.0.1:8000/> (redirects to the UI at `/app/`). Swagger stays at `/docs`.
There is no frontend build step or Node requirement: the UI is plain ES modules.

### Demo walkthrough

0. Optionally use **Add vendor** on the Overview page to register a new vendor; it will have no
   evidence and a review ends `EVIDENCE_DEFICIENT`.
1. **Acme Corp** (Overview -> Start task). The run pauses in *Waiting approval*; inspect the tool
   calls, verified requirements and timeline. Refresh: the pending run persists.
2. Approvals -> open Acme -> Approve -> confirm. The run resumes and becomes *COMPLETED*.
   Reject instead to see `APPROVAL_REJECTED`. A second decision is refused (409).
3. **Globex Industries**: expired SOC 2, unsigned DPA, partial questionnaire ->
   `EVIDENCE_DEFICIENT`; the vendor is never marked compliant, and approving is blocked.
4. **Hooli**: seeded evidence + seeded approval (a *demo fixture*, not a real officer decision)
   -> `COMPLETED`. To replay the demo, delete `complianceops.db` and re-seed.

### API

| Route | Purpose |
|---|---|
| `GET /health`, `/ready`, `/meta` | liveness; DB + LLM-configured flag; provider name and demo actor |
| `GET /vendors`, `/vendors/{id}` | live requirement evaluation, evidence, reviews, runs |
| `POST /vendors` | create a vendor (`name`, optional `description`); returns `201` with the vendor detail |
| `POST /runs`, `GET /runs`, `GET /runs/{id}` | start (sync), list (`status`, `vendor_id`, `limit`), detail with tool calls + audit |
| `GET /approvals`, `/approvals/{id}` | queue (`status=PENDING`) and request detail |
| `POST /approvals/{id}/decision` | human `APPROVED`/`REJECTED` (+ comment) |

Errors are `{"detail", "code"}` (422 adds `errors`). 404 not found, 409 `vendor_exists` /
`approval_not_pending` / `evidence_deficient` / `run_in_progress`, 422 validation, 503
`llm_not_configured`.

A vendor created with `POST /vendors` (or the **Add vendor** button on the Overview page) has no
evidence, review or approval. The policy engine reports every requirement as missing, so it is
evidence-deficient; starting a task for it ends in `FAILED` / `EVIDENCE_DEFICIENT`. Vendor names
are unique, compared case-insensitively. Evidence cannot yet be added through the API or UI (see
Limitations), so the demo vendors are the only ones that can reach a compliant state.

### Human-approval semantics and security boundary

- Only `POST /approvals/{id}/decision` moves an approval out of `PENDING`; it is not a registered
  tool and the agent principal is read-only.
- The acting identity is the server's `DEMO_ACTOR` with role `compliance_officer`. The client
  sends only decision and comment. **This is not authentication**: anyone who can reach the API
  can decide. Do not expose it publicly.
- Approving requires currently valid evidence; rejecting is always allowed while pending.
  Decisions are final; concurrent decisions are serialized (exactly one wins).

## Checks

```bash
cd backend
pytest              # no API key or network needed; the model is mocked
ruff check .
ruff format --check .
mypy app
```
