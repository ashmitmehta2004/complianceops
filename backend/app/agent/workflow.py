"""Run lifecycle rules shared by the agent runtime and the approval-resume path.

Everything here works on a caller-provided session so callers choose the transaction boundary.
The verdict is computed only from persisted data by the deterministic policy engine; the model
is never consulted.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.compliance import ComplianceEvaluation
from app.domain.enums import ApprovalDecision, RunOutcome, WorkflowStatus
from app.persistence.database import session_scope
from app.persistence.models import AgentRun, Approval, AuditEvent, ComplianceReview, utcnow
from app.tools.compliance import evaluate_review

RUNTIME_ACTOR = "runtime"

ALLOWED_TRANSITIONS: dict[WorkflowStatus, set[WorkflowStatus]] = {
    WorkflowStatus.RECEIVED: {WorkflowStatus.PLANNING, WorkflowStatus.FAILED},
    WorkflowStatus.PLANNING: {WorkflowStatus.EXECUTING, WorkflowStatus.FAILED},
    WorkflowStatus.EXECUTING: {WorkflowStatus.VERIFYING, WorkflowStatus.FAILED},
    WorkflowStatus.VERIFYING: {
        WorkflowStatus.WAITING_APPROVAL,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
    },
    # Resume after a human decision re-verifies; an abandoned wait may also fail.
    WorkflowStatus.WAITING_APPROVAL: {WorkflowStatus.VERIFYING, WorkflowStatus.FAILED},
}

IN_FLIGHT = (
    WorkflowStatus.RECEIVED,
    WorkflowStatus.PLANNING,
    WorkflowStatus.EXECUTING,
    WorkflowStatus.VERIFYING,
)


class InvalidTransition(RuntimeError):
    pass


def audit(session: Session, run_id: str, event_type: str, actor: str, **details: Any) -> None:
    session.add(AuditEvent(run_id=run_id, event_type=event_type, actor=actor, details=details))


def transition_run(session: Session, run: AgentRun, new: WorkflowStatus, **details: Any) -> None:
    """Move a run to `new` if the state machine allows it, auditing the change.

    `completed_at` is set exactly when the run reaches a terminal state."""
    if new not in ALLOWED_TRANSITIONS.get(run.status, set()):
        raise InvalidTransition(f"illegal transition {run.status} -> {new}")
    old = run.status
    run.status = new
    if new.is_terminal:
        run.completed_at = utcnow()
    audit(
        session,
        run.id,
        "status_changed",
        RUNTIME_ACTOR,
        **{"from": old.value, "to": new.value, **details},
    )


@dataclass(frozen=True)
class Verdict:
    status: WorkflowStatus
    outcome: RunOutcome
    evaluation: ComplianceEvaluation
    approval_id: str | None
    summary: str


def verify_review(
    session: Session, review_id: str, now: datetime, run_id: str | None = None
) -> Verdict:
    """Fresh deterministic verdict for a review from persisted documents and approvals.

    Side effects are limited to bookkeeping: if evidence is valid and no approval record exists a
    PENDING request is created (never a decision), and the review's status is updated."""
    evaluation = evaluate_review(session, review_id, now)
    approvals = list(session.scalars(select(Approval).where(Approval.review_id == review_id)))
    latest = max(approvals, key=lambda a: (a.requested_at, a.id), default=None)
    approval_id = latest.id if latest else None
    if evaluation.can_finalize:
        status, outcome = WorkflowStatus.COMPLETED, RunOutcome.COMPLIANT_APPROVED
    elif not evaluation.evidence_compliant:
        status, outcome = WorkflowStatus.FAILED, RunOutcome.EVIDENCE_DEFICIENT
    elif latest is not None and latest.decision is ApprovalDecision.REJECTED:
        status, outcome = WorkflowStatus.FAILED, RunOutcome.APPROVAL_REJECTED
    else:
        status, outcome = WorkflowStatus.WAITING_APPROVAL, RunOutcome.AWAITING_APPROVAL
        if latest is None:
            approval_id = _request_approval(session, review_id, run_id)
    review = session.get(ComplianceReview, review_id)
    if review is None:
        raise RuntimeError("review disappeared during verification")
    review.status = status
    return Verdict(status, outcome, evaluation, approval_id, summarize(outcome, evaluation))


def _request_approval(session: Session, review_id: str, run_id: str | None) -> str:
    """Create the single PENDING request for a review. If a concurrent run won the race (unique
    index), reuse its request instead of failing."""
    try:
        with session.begin_nested():
            request = Approval(review_id=review_id, run_id=run_id)
            session.add(request)
            session.flush()
            return request.id
    except IntegrityError:
        return (
            session.scalars(
                select(Approval).where(
                    Approval.review_id == review_id, Approval.decision == ApprovalDecision.PENDING
                )
            )
            .one()
            .id
        )


def record_verdict(session: Session, run: AgentRun, review_id: str, verdict: Verdict) -> None:
    """Persist the verification, final run fields and the closing transition (run must be
    VERIFYING). One call = one atomic outcome."""
    audit(
        session,
        run.id,
        "verification",
        RUNTIME_ACTOR,
        evidence_compliant=verdict.evaluation.evidence_compliant,
        approval_satisfied=verdict.evaluation.approval_satisfied,
        can_finalize=verdict.evaluation.can_finalize,
        outcome=verdict.outcome.value,
        approval_id=verdict.approval_id,
        summary=verdict.summary,
        requirements=[r.model_dump(mode="json") for r in verdict.evaluation.results],
    )
    if verdict.outcome is RunOutcome.AWAITING_APPROVAL:
        audit(
            session,
            run.id,
            "approval_pending",
            RUNTIME_ACTOR,
            approval_id=verdict.approval_id,
            review_id=review_id,
        )
    run.outcome = verdict.outcome.value
    run.summary = verdict.summary
    run.review_id = review_id
    run.approval_id = verdict.approval_id
    transition_run(session, run, verdict.status, outcome=verdict.outcome.value)


def summarize(outcome: RunOutcome, evaluation: ComplianceEvaluation) -> str:
    lines = [
        f"{r.requirement.value}: {r.status.value} ({r.reason.value})" for r in evaluation.results
    ]
    headline = {
        RunOutcome.COMPLIANT_APPROVED: "Verified: evidence valid and Compliance Officer approved.",
        RunOutcome.AWAITING_APPROVAL: (
            "Evidence verified valid. Awaiting Compliance Officer approval (human action)."
        ),
        RunOutcome.EVIDENCE_DEFICIENT: (
            "Not compliant: evidence is deficient; not ready for approval."
        ),
        RunOutcome.APPROVAL_REJECTED: "Evidence valid but approval was rejected by a human.",
    }[outcome]
    return headline + " " + "; ".join(lines)


def recover_interrupted_runs(factory: sessionmaker[Session]) -> int:
    """Fail runs that were in flight when the process stopped.

    Runs execute synchronously inside a request, so at startup any in-flight run belongs to a
    dead process. WAITING_APPROVAL runs are left alone: they resume when a human decides."""
    with session_scope(factory) as s:
        runs = list(s.scalars(select(AgentRun).where(AgentRun.status.in_(IN_FLIGHT))))
        for run in runs:
            reason = "process stopped before the run finished"
            run.outcome = RunOutcome.INTERRUPTED.value
            run.summary = reason
            transition_run(s, run, WorkflowStatus.FAILED, outcome=run.outcome, reason=reason)
        return len(runs)
