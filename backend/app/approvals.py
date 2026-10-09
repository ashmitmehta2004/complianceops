"""Human approval decisions: validate, record, re-verify, resume.

This is the only code path that moves an approval out of PENDING, and it is reachable only from
the HTTP API (a human action). It is deliberately not a registered tool, so the model has no way
to call it. Everything happens in one transaction:

  1. the request must still be PENDING (a conditional UPDATE makes concurrent decisions safe),
  2. approving requires the evidence to be valid *now* (approval cannot paper over deficiencies),
  3. the decision, actor and timestamp are persisted,
  4. the policy engine re-evaluates persisted state and each waiting run is finalized from that
     verdict, not from the decision alone.

If any step fails the whole decision rolls back; there is no half-recorded state.
"""

from dataclasses import dataclass

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agent.workflow import audit, record_verdict, transition_run, verify_review
from app.domain.enums import ApprovalDecision, WorkflowStatus
from app.persistence.database import session_scope
from app.persistence.models import AgentRun, Approval, utcnow
from app.tools.compliance import evaluate_review

OFFICER_ROLE = "compliance_officer"  # must equal CompliancePolicy.compliance_officer_role
HUMAN_ACTOR_KIND = "human"


class ApprovalError(Exception):
    code = "approval_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ApprovalNotFound(ApprovalError):
    code = "approval_not_found"


class ApprovalNotPending(ApprovalError):
    code = "approval_not_pending"


class EvidenceDeficient(ApprovalError):
    code = "evidence_deficient"


@dataclass(frozen=True)
class DecisionResult:
    approval_id: str
    decision: ApprovalDecision
    run_ids: list[str]  # runs finalized (resumed) by this decision


def decide_approval(
    factory: sessionmaker[Session],
    approval_id: str,
    decision: ApprovalDecision,
    actor: str,
    comment: str | None = None,
) -> DecisionResult:
    if decision is ApprovalDecision.PENDING:
        raise ValueError("a decision must be APPROVED or REJECTED")
    refused_runs: list[str] = []
    try:
        with session_scope(factory) as s:
            return _decide(s, approval_id, decision, actor, comment, refused_runs)
    except EvidenceDeficient as exc:
        # The refusal itself is evidence worth keeping; the failed decision rolled back above.
        with session_scope(factory) as s:
            for run_id in refused_runs:
                audit(
                    s,
                    run_id,
                    "approval_decision_refused",
                    actor,
                    approval_id=approval_id,
                    attempted=decision.value,
                    reason=exc.message,
                )
        raise


def _decide(
    s: Session,
    approval_id: str,
    decision: ApprovalDecision,
    actor: str,
    comment: str | None,
    refused_runs: list[str],
) -> DecisionResult:
    approval = s.get(Approval, approval_id)
    if approval is None:
        raise ApprovalNotFound("approval not found")
    if approval.decision is not ApprovalDecision.PENDING:
        raise ApprovalNotPending(
            f"approval already {approval.decision.value.lower()}; decisions are final"
        )

    review_id = approval.review_id
    runs = _linked_runs(s, approval)
    if decision is ApprovalDecision.APPROVED:
        evaluation = evaluate_review(s, review_id, utcnow())
        if not evaluation.evidence_compliant:
            refused_runs.extend(r.id for r in runs)
            raise EvidenceDeficient(
                "evidence is not currently valid; approval cannot make this vendor compliant"
            )

    now = utcnow()
    claimed = s.execute(
        update(Approval)
        .where(Approval.id == approval_id, Approval.decision == ApprovalDecision.PENDING)
        .values(
            decision=decision,
            approver=OFFICER_ROLE,
            decided_by=actor,
            comment=comment,
            resolved_at=now,
        )
    )
    if getattr(claimed, "rowcount", 0) != 1:  # lost a race with a concurrent decision
        raise ApprovalNotPending("approval was decided concurrently; decisions are final")
    s.refresh(approval)

    finalized: list[str] = []
    for run in runs:
        audit(
            s,
            run.id,
            "approval_decided",
            actor,
            actor_kind=HUMAN_ACTOR_KIND,
            approval_id=approval_id,
            decision=decision.value,
            role=OFFICER_ROLE,
            comment=comment,
        )
        if run.status is WorkflowStatus.WAITING_APPROVAL:
            transition_run(
                s, run, WorkflowStatus.VERIFYING, reason="human decision recorded; re-verifying"
            )
            verdict = verify_review(s, review_id, now, run_id=run.id)
            record_verdict(s, run, review_id, verdict)
            finalized.append(run.id)
    if not finalized:
        verify_review(s, review_id, now)  # keep the review's status consistent
    return DecisionResult(approval_id=approval_id, decision=decision, run_ids=finalized)


def _linked_runs(s: Session, approval: Approval) -> list[AgentRun]:
    """Runs that requested this approval or are waiting on this review."""
    return list(
        s.scalars(
            select(AgentRun)
            .where(
                or_(
                    AgentRun.approval_id == approval.id,
                    AgentRun.id == approval.run_id,
                    (AgentRun.review_id == approval.review_id)
                    & (AgentRun.status == WorkflowStatus.WAITING_APPROVAL),
                )
            )
            .order_by(AgentRun.started_at, AgentRun.id)
        )
    )
