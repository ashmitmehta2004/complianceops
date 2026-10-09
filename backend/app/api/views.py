"""Assemble API views from persisted rows. Read-only; no business decisions are made here."""

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.schemas import (
    ApprovalView,
    AuditEventView,
    DocumentView,
    RequirementView,
    ReviewView,
    RunSummaryView,
    RunView,
    ToolCallView,
    VendorDetailView,
    VendorSummaryView,
)
from app.domain.compliance import ComplianceEvaluation, RequirementResult
from app.domain.enums import ApprovalDecision, RunOutcome, ToolCallStatus, WorkflowStatus
from app.domain.explanations import LABELS, explain
from app.persistence.models import (
    AgentRun,
    Approval,
    ComplianceReview,
    Document,
    Vendor,
    utcnow,
)
from app.tools.compliance import evaluate_review, evaluate_vendor

SEED_FIXTURE_ACTOR = "seed-fixture"


def requirement_view(r: RequirementResult) -> RequirementView:
    return RequirementView(
        requirement=r.requirement,
        label=LABELS[r.requirement],
        status=r.status,
        reason=r.reason,
        explanation=explain(r),
        source_id=r.source_id,
        period_end=r.period_end,
        expires_on=r.expires_on,
    )


def requirement_views(evaluation: ComplianceEvaluation) -> list[RequirementView]:
    return [requirement_view(r) for r in evaluation.results]


def document_view(d: Document) -> DocumentView:
    return DocumentView(
        id=d.id,
        document_type=d.document_type,
        filename=d.filename,
        source=d.source,
        created_at=d.created_at,
        facts=d.metadata_,
    )


# ---- runs ----


def _vendor_name(session: Session, vendor_id: str | None) -> str | None:
    vendor = session.get(Vendor, vendor_id) if vendor_id else None
    return vendor.name if vendor else None


def _legacy_fields(run: AgentRun) -> dict[str, str | None]:
    """Runs created before the outcome/vendor columns existed are read from their audit trail."""
    by_type = {e.event_type: e for e in run.audit_events}
    context = by_type.get("context_loaded")
    verification = by_type.get("verification")
    final = next(
        (
            e
            for e in reversed(run.audit_events)
            if e.event_type == "status_changed" and e.details.get("outcome")
        ),
        None,
    )
    return {
        "outcome": final.details["outcome"] if final else None,
        "vendor_id": context.details.get("vendor_id") if context else None,
        "review_id": context.details.get("review_id") if context else None,
        "approval_id": verification.details.get("approval_id") if verification else None,
        "summary": (
            verification.details.get("summary")
            if verification
            else (final.details.get("reason") if final else None)
        ),
    }


def run_summary(session: Session, run: AgentRun) -> RunSummaryView:
    legacy = _legacy_fields(run) if run.outcome is None and run.status.is_terminal else {}
    outcome_raw = run.outcome or legacy.get("outcome")
    outcome = RunOutcome(outcome_raw) if outcome_raw else None
    vendor_id = run.vendor_id or legacy.get("vendor_id")
    return RunSummaryView(
        run_id=run.id,
        goal=run.goal,
        status=run.status,
        outcome=outcome,
        summary=run.summary or legacy.get("summary"),
        vendor_id=vendor_id,
        vendor_name=_vendor_name(session, vendor_id),
        approval_id=run.approval_id or legacy.get("approval_id"),
        human_action_required=(
            run.status is WorkflowStatus.WAITING_APPROVAL
            or (run.status.is_terminal and outcome is not RunOutcome.COMPLIANT_APPROVED)
        ),
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


def build_run_view(session: Session, run: AgentRun) -> RunView:
    """A run's full state purely from persisted rows (readable after a restart)."""
    summary = run_summary(session, run)
    events = list(run.audit_events)
    verification = next((e for e in reversed(events) if e.event_type == "verification"), None)
    requirements = (
        [
            requirement_view(RequirementResult.model_validate(r))
            for r in verification.details.get("requirements", [])
        ]
        if verification
        else []
    )
    context = next((e for e in events if e.event_type == "context_loaded"), None)
    return RunView(
        **summary.model_dump(),
        review_id=run.review_id or (context.details.get("review_id") if context else None),
        model_summary=run.model_summary,
        requirements=requirements,
        tool_calls=[
            ToolCallView(
                id=c.id,
                tool_name=c.tool_name,
                status=c.status,
                timestamp=c.timestamp,
                error_code=(
                    c.output["error"]["code"]
                    if c.status is ToolCallStatus.FAILED and c.output
                    else None
                ),
                input=c.input,
                output=c.output,
            )
            for c in run.tool_calls
        ],
        audit_events=[
            AuditEventView(
                event_type=e.event_type, actor=e.actor, timestamp=e.timestamp, details=e.details
            )
            for e in events
        ],
    )


# ---- approvals ----


def approval_view(
    session: Session, approval: Approval, now: datetime | None = None
) -> ApprovalView:
    now = now or utcnow()
    review = session.get(ComplianceReview, approval.review_id)
    assert review is not None
    vendor = session.get(Vendor, review.vendor_id)
    assert vendor is not None
    evaluation = evaluate_review(session, review.id, now)
    pending = approval.decision is ApprovalDecision.PENDING
    blocked = None
    if pending and not evaluation.evidence_compliant:
        blocked = (
            "Evidence is not currently valid. Approving would not make this vendor compliant, "
            "so the approval cannot be granted until the evidence is fixed."
        )
    run_ids = session.scalars(
        select(AgentRun.id)
        .where(or_(AgentRun.approval_id == approval.id, AgentRun.id == approval.run_id))
        .order_by(AgentRun.started_at, AgentRun.id)
    ).all()
    documents = session.scalars(
        select(Document).where(Document.vendor_id == vendor.id).order_by(Document.created_at)
    ).all()
    return ApprovalView(
        id=approval.id,
        review_id=review.id,
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        decision=approval.decision,
        requested_at=approval.requested_at,
        resolved_at=approval.resolved_at,
        role=approval.approver,
        decided_by=approval.decided_by,
        comment=approval.comment,
        is_fixture=approval.decided_by == SEED_FIXTURE_ACTOR,
        run_ids=list(run_ids),
        evidence_compliant=evaluation.evidence_compliant,
        can_approve=pending and evaluation.evidence_compliant,
        blocked_reason=blocked,
        requirements=requirement_views(evaluation),
        documents=[document_view(d) for d in documents],
    )


# ---- vendors ----


def _latest_run(session: Session, vendor_id: str) -> AgentRun | None:
    return session.scalars(
        select(AgentRun)
        .where(AgentRun.vendor_id == vendor_id)
        .order_by(AgentRun.started_at.desc(), AgentRun.id)
        .limit(1)
    ).first()


def vendor_summary(
    session: Session, vendor: Vendor, now: datetime | None = None
) -> VendorSummaryView:
    now = now or utcnow()
    review, evaluation = evaluate_vendor(session, vendor.id, now)
    pending = None
    if review is not None:
        pending = session.scalars(
            select(Approval.id).where(
                Approval.review_id == review.id, Approval.decision == ApprovalDecision.PENDING
            )
        ).first()
    last = _latest_run(session, vendor.id)
    return VendorSummaryView(
        id=vendor.id,
        name=vendor.name,
        description=vendor.metadata_.get("description") or vendor.metadata_.get("seed_scenario"),
        review_id=review.id if review else None,
        review_status=review.status if review else None,
        evidence_compliant=evaluation.evidence_compliant,
        approval_satisfied=evaluation.approval_satisfied,
        pending_approval_id=pending,
        requirements=requirement_views(evaluation),
        last_run=run_summary(session, last) if last else None,
    )


def vendor_detail(
    session: Session, vendor: Vendor, now: datetime | None = None
) -> VendorDetailView:
    now = now or utcnow()
    summary = vendor_summary(session, vendor, now)
    documents = session.scalars(
        select(Document).where(Document.vendor_id == vendor.id).order_by(Document.created_at)
    ).all()
    reviews = session.scalars(
        select(ComplianceReview)
        .where(ComplianceReview.vendor_id == vendor.id)
        .order_by(ComplianceReview.created_at.desc())
    ).all()
    runs = session.scalars(
        select(AgentRun)
        .where(AgentRun.vendor_id == vendor.id)
        .order_by(AgentRun.started_at.desc(), AgentRun.id)
        .limit(50)
    ).all()
    return VendorDetailView(
        **summary.model_dump(),
        documents=[document_view(d) for d in documents],
        reviews=[
            ReviewView(
                id=r.id,
                status=r.status,
                created_at=r.created_at,
                updated_at=r.updated_at,
                approvals=[
                    approval_view(session, a, now)
                    for a in sorted(r.approvals, key=lambda a: (a.requested_at, a.id))
                ],
            )
            for r in reviews
        ],
        runs=[run_summary(session, r) for r in runs],
    )
