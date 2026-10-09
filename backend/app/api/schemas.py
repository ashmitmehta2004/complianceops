"""HTTP request/response schemas. Internal exceptions and secrets never appear here."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.compliance import ReasonCode, RequirementKey, RequirementStatus
from app.domain.enums import (
    ApprovalDecision,
    RunOutcome,
    ToolCallStatus,
    WorkflowStatus,
)


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: str = Field(min_length=1, max_length=200, examples=["Acme Corp"])


class CreateVendorRequest(BaseModel):
    """A vendor record only. No evidence, review or approval is created with it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200, examples=["Umbrella Ltd"])
    description: str | None = Field(default=None, max_length=500)


class DecisionRequest(BaseModel):
    """The client supplies only the decision and an optional comment. The acting identity and
    role are set by the server."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVED", "REJECTED"]
    comment: str | None = Field(default=None, max_length=1000)


class RequirementView(BaseModel):
    requirement: RequirementKey
    label: str
    status: RequirementStatus
    reason: ReasonCode
    explanation: str  # restates the deterministic result in words
    source_id: str | None = None
    period_end: date | None = None
    expires_on: date | None = None


class ToolCallView(BaseModel):
    id: str
    tool_name: str
    status: ToolCallStatus
    timestamp: datetime
    error_code: str | None = None
    input: dict[str, Any] = {}
    output: dict[str, Any] | None = None


class AuditEventView(BaseModel):
    event_type: str
    actor: str
    timestamp: datetime
    details: dict[str, Any]


class RunSummaryView(BaseModel):
    run_id: str
    goal: str
    status: WorkflowStatus
    outcome: RunOutcome | None = None
    summary: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    approval_id: str | None = None
    human_action_required: bool
    started_at: datetime
    completed_at: datetime | None = None


class RunView(RunSummaryView):
    review_id: str | None = None
    model_summary: str | None = None  # untrusted model text; never used for decisions
    requirements: list[RequirementView] = []
    tool_calls: list[ToolCallView] = []
    audit_events: list[AuditEventView] = []


class DocumentView(BaseModel):
    id: str
    document_type: str
    filename: str
    source: str
    created_at: datetime
    facts: dict[str, Any]


class ApprovalView(BaseModel):
    id: str
    review_id: str
    vendor_id: str
    vendor_name: str
    decision: ApprovalDecision
    requested_at: datetime
    resolved_at: datetime | None = None
    role: str | None = None
    decided_by: str | None = None
    comment: str | None = None
    is_fixture: bool  # seeded demo data, not a real officer decision
    run_ids: list[str] = []
    evidence_compliant: bool
    can_approve: bool  # pending AND evidence currently valid
    blocked_reason: str | None = None
    requirements: list[RequirementView] = []
    documents: list[DocumentView] = []


class DecisionResponse(BaseModel):
    approval: ApprovalView
    resumed_runs: list[RunSummaryView]


class ReviewView(BaseModel):
    id: str
    status: WorkflowStatus
    created_at: datetime
    updated_at: datetime
    approvals: list[ApprovalView]


class VendorSummaryView(BaseModel):
    id: str
    name: str
    description: str | None = None
    review_id: str | None = None
    review_status: WorkflowStatus | None = None
    evidence_compliant: bool
    approval_satisfied: bool
    pending_approval_id: str | None = None
    requirements: list[RequirementView]
    last_run: RunSummaryView | None = None


class VendorDetailView(VendorSummaryView):
    documents: list[DocumentView]
    reviews: list[ReviewView]
    runs: list[RunSummaryView]


class ErrorView(BaseModel):
    detail: str
    code: str | None = None
