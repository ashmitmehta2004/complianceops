"""Endpoints to start a compliance run and read persisted run state."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.agent.llm import LLMError
from app.agent.runtime import AgentRuntime
from app.agent.workflow import IN_FLIGHT
from app.api.deps import LLMFactoryDep, SessionFactoryDep
from app.api.errors import ApiError
from app.api.schemas import ErrorView, RunSummaryView, RunView, StartRunRequest
from app.api.views import build_run_view, run_summary
from app.domain.enums import WorkflowStatus
from app.persistence.models import AgentRun, Vendor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorView},
    409: {"model": ErrorView},
    503: {"model": ErrorView},
}


@router.post(
    "",
    response_model=RunView,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
    summary="Start a compliance review run for a vendor",
    description=(
        "Runs the agent synchronously and returns the finished run. A 201 means a run record "
        "was created; check `status` and `outcome` for the result. `COMPLETED` is only possible "
        "when deterministic verification finds valid evidence AND a recorded Compliance Officer "
        "approval. Valid evidence without approval ends in `WAITING_APPROVAL`; a human decision "
        "via `POST /approvals/{id}/decision` then resumes it. 409 if an identical run is "
        "already in progress."
    ),
)
def start_run(
    body: StartRunRequest, factory: SessionFactoryDep, make_llm: LLMFactoryDep
) -> RunView:
    with factory() as session:
        vendor = session.scalars(
            select(Vendor).where(func.lower(Vendor.name) == body.vendor_name.strip().lower())
        ).one_or_none()
        if vendor is None:
            raise ApiError(404, "vendor_not_found", "vendor not found")
        goal = f"Review {vendor.name}'s compliance status and prepare it for approval."
        in_flight = session.scalars(
            select(AgentRun.id).where(AgentRun.goal == goal, AgentRun.status.in_(IN_FLIGHT))
        ).first()
    if in_flight is not None:
        raise ApiError(409, "run_in_progress", f"a run for {vendor.name} is already in progress")
    try:
        llm = make_llm()
    except LLMError as exc:
        # Server configuration problem. The message names env vars only, never values, so it is
        # safe to log; the HTTP response stays generic.
        logger.warning("LLM provider not configured: %s", exc)
        raise ApiError(503, "llm_not_configured", "LLM is not configured on the server") from None

    result = AgentRuntime(llm, factory).run(goal)
    with factory() as session:
        run = session.get(AgentRun, result.run_id)
        assert run is not None
        return build_run_view(session, run)


@router.get(
    "",
    response_model=list[RunSummaryView],
    summary="List runs, newest first",
)
def list_runs(
    factory: SessionFactoryDep,
    run_status: Annotated[WorkflowStatus | None, Query(alias="status")] = None,
    vendor_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunSummaryView]:
    stmt = select(AgentRun).order_by(AgentRun.started_at.desc(), AgentRun.id).limit(limit)
    if run_status is not None:
        stmt = stmt.where(AgentRun.status == run_status)
    if vendor_id is not None:
        stmt = stmt.where(AgentRun.vendor_id == vendor_id)
    with factory() as session:
        return [run_summary(session, r) for r in session.scalars(stmt)]


@router.get(
    "/{run_id}",
    response_model=RunView,
    responses={404: {"model": ErrorView}},
    summary="Get a run's status, result, tool calls and audit trail",
)
def get_run(run_id: str, factory: SessionFactoryDep) -> RunView:
    with factory() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise ApiError(404, "run_not_found", "run not found")
        return build_run_view(session, run)
