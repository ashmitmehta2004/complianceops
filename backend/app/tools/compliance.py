"""Read-only tool exposing the deterministic compliance evaluator.

Persisted rows are mapped to the evaluator's input models here, explicitly: the evaluator never
sees ORM objects and the LLM never sees the evaluator's inputs, only its structured result.
"""

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.compliance import ApprovalRecord, ComplianceEvaluation, SubmittedDocument
from app.domain.compliance_rules import evaluate_compliance
from app.persistence.models import Approval, ComplianceReview, Document, utcnow
from app.tools.base import ToolContext, ToolDefinition, ToolErrorCode, ToolFailure, ToolInput
from app.tools.permissions import Permission
from app.tools.registry import ToolRegistry


def _evaluate(
    session: Session, vendor_id: str, approvals: Sequence[Approval], now: datetime
) -> ComplianceEvaluation:
    documents = session.scalars(select(Document).where(Document.vendor_id == vendor_id))
    return evaluate_compliance(
        documents=[
            SubmittedDocument(
                id=doc.id,
                document_type=doc.document_type,
                submitted_at=doc.created_at,
                facts=doc.metadata_,
            )
            for doc in documents
        ],
        approvals=[
            ApprovalRecord(
                id=a.id,
                decision=a.decision,
                approver_role=a.approver,
                requested_at=a.requested_at,
            )
            for a in approvals
        ],
        evaluated_at=now,
    )


def evaluate_review(session: Session, review_id: str, now: datetime) -> ComplianceEvaluation:
    """Evaluate a review from its persisted documents and approvals.

    Document.metadata_ holds the extracted facts. Approval.approver holds the approver's role
    (the schema has no separate role column), which the evaluator compares to the policy role.
    Raises LookupError if the review does not exist.
    """
    review = session.get(ComplianceReview, review_id)
    if review is None:
        raise LookupError(review_id)
    approvals = session.scalars(select(Approval).where(Approval.review_id == review_id)).all()
    return _evaluate(session, review.vendor_id, approvals, now)


def evaluate_vendor(
    session: Session, vendor_id: str, now: datetime
) -> tuple[ComplianceReview | None, ComplianceEvaluation]:
    """Evaluate a vendor against its latest review (documents only if it has no review)."""
    review = session.scalars(
        select(ComplianceReview)
        .where(ComplianceReview.vendor_id == vendor_id)
        .order_by(ComplianceReview.created_at.desc(), ComplianceReview.id)
        .limit(1)
    ).first()
    if review is None:
        return None, _evaluate(session, vendor_id, [], now)
    return review, evaluate_review(session, review.id, now)


class EvaluateComplianceInput(ToolInput):
    review_id: str = Field(min_length=1)


class EvaluateComplianceOutput(BaseModel):
    review_id: str
    vendor_id: str
    evaluation: ComplianceEvaluation


def evaluate_vendor_compliance(
    args: EvaluateComplianceInput, ctx: ToolContext
) -> EvaluateComplianceOutput:
    review = ctx.session.get(ComplianceReview, args.review_id)
    if review is None:
        raise ToolFailure(ToolErrorCode.NOT_FOUND, "review not found")
    evaluation = evaluate_review(ctx.session, review.id, utcnow())
    return EvaluateComplianceOutput(
        review_id=review.id, vendor_id=review.vendor_id, evaluation=evaluation
    )


def register_compliance_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="evaluate_vendor_compliance",
            description=(
                "Run the deterministic compliance policy engine over a review's persisted "
                "documents and approvals. Returns per-requirement status and reason codes. "
                "Read-only: it does not change the review or request approval."
            ),
            input_model=EvaluateComplianceInput,
            output_model=EvaluateComplianceOutput,
            required_permission=Permission.COMPLIANCE_EVALUATE,
            handler=evaluate_vendor_compliance,
        )
    )
