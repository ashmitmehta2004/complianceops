# Design notes

Architecture decisions and the reasoning behind them. The README covers setup and usage.

## Request path of `POST /runs`

`api/runs.py::start_run` -> vendor lookup by name -> `llm_from_env` -> `AgentRuntime.run(goal)`
-> creates an `AgentRun` -> `_resolve_target` (the runtime, not the model, picks the vendor and
its latest review, creating one if none exists) -> model/tool loop through `ToolRuntime`
(permission and schema validation) -> `_verify_and_finalize` (fresh deterministic evaluation;
may create a `PENDING` approval) -> `RunView` rebuilt from persisted rows.

## Design decisions

- **Deterministic verification.** Requirement statuses, the outcome and completion come only
  from the policy engine over persisted rows. Model text is stored as an unverified
  `model_summary`.
- **Approval is never a tool.** The agent principal is read-only. A decision is a separate HTTP
  action (`POST /approvals/{id}/decision`) performed by a server-configured demo actor. This is
  not authentication.
- **Resume is deterministic.** After a human decision the backend re-runs the policy engine on
  persisted state and finalizes the waiting run. Re-invoking the model would add nothing: it
  cannot approve, and the outcome never depends on its text.
- **Rejection is final for that review.** A rejected approval is not silently re-requested; a
  new review would be needed (not implemented).
- **Vendor creation records identity only.** `POST /vendors` stores a name and optional
  description. It creates no documents, review or approval, so a new vendor is evaluated as
  all-`MISSING` (evidence-deficient) until evidence exists. The first run creates its review.
- **Verification and finalization are atomic.** Verify, record the result and make the final
  status transition in one transaction.
- **At most one open approval per review**, enforced by a partial unique index and a
  savepoint/re-select on conflict.
- **Interrupted runs are recovered.** On startup, runs left in flight by a dead process become
  `FAILED` / `INTERRUPTED`; `WAITING_APPROVAL` runs survive.
- **Additive schema upgrades.** `persistence/migrations.py` adds missing columns and indexes
  idempotently. Destructive changes would need Alembic.
- **Build-less frontend.** The UI is plain HTML/CSS/ES modules served by the backend, with no
  Node toolchain. As a consequence there is no frontend lint, typecheck or unit-test setup.
