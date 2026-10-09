"""Approval queue and the human decision action.

The decision endpoint is the only way an approval leaves PENDING. It is a human-facing HTTP
action performed as the server-configured demo actor; it is not a tool the agent can call.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DemoActorDep, SessionFactoryDep
from app.api.errors import ApiError
from app.api.schemas import (
    ApprovalView,
    DecisionRequest,
    DecisionResponse,
    ErrorView,
)
from app.api.views import approval_view, run_summary
from app.approvals import ApprovalNotFound, ApprovalNotPending, EvidenceDeficient, decide_approval
from app.domain.enums import ApprovalDecision
from app.persistence.models import AgentRun, Approval

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalView], summary="List approval requests")
def list_approvals(
    factory: SessionFactoryDep,
    decision: Annotated[ApprovalDecision | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ApprovalView]:
    stmt = select(Approval).order_by(Approval.requested_at.desc(), Approval.id).limit(limit)
    if decision is not None:
        stmt = stmt.where(Approval.decision == decision)
    with factory() as session:
        return [approval_view(session, a) for a in session.scalars(stmt)]


@router.get(
    "/{approval_id}",
    response_model=ApprovalView,
    responses={404: {"model": ErrorView}},
    summary="Approval request with vendor, evidence and current verification",
)
def get_approval(approval_id: str, factory: SessionFactoryDep) -> ApprovalView:
    with factory() as session:
        approval = session.get(Approval, approval_id)
        if approval is None:
            raise ApiError(404, "approval_not_found", "approval not found")
        return approval_view(session, approval)


@router.post(
    "/{approval_id}/decision",
    response_model=DecisionResponse,
    responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
    summary="Approve or reject a pending request (human action)",
    description=(
        "Records the decision as the server-configured demo actor (not authentication), "
        "re-verifies compliance from persisted state and finalizes runs waiting on this "
        "approval. 409 `approval_not_pending` if already decided; 409 `evidence_deficient` if "
        "approving while evidence is not valid. Rejecting is always allowed while pending."
    ),
)
def decide(
    approval_id: str,
    body: DecisionRequest,
    factory: SessionFactoryDep,
    actor: DemoActorDep,
) -> DecisionResponse:
    comment = body.comment.strip() if body.comment and body.comment.strip() else None
    try:
        result = decide_approval(
            factory, approval_id, ApprovalDecision(body.decision), actor.name, comment
        )
    except ApprovalNotFound as exc:
        raise ApiError(404, exc.code, exc.message) from None
    except (ApprovalNotPending, EvidenceDeficient) as exc:
        raise ApiError(409, exc.code, exc.message) from None
    with factory() as session:
        approval = session.get(Approval, approval_id)
        assert approval is not None
        runs = [session.get(AgentRun, rid) for rid in result.run_ids]
        return DecisionResponse(
            approval=approval_view(session, approval),
            resumed_runs=[run_summary(session, r) for r in runs if r is not None],
        )
