"""SQLAlchemy models. These are persistence-only and never exposed to the LLM."""

import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect, Enum, ForeignKey, Index, Text, TypeDecorator, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.domain.enums import ApprovalDecision, EvidenceStatus, ToolCallStatus, WorkflowStatus
from app.persistence.database import Base


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes; SQLite would otherwise return naive values."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed; use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


_order_lock = threading.Lock()
_last_ordered = 0


def new_ordered_id() -> str:
    """A process-monotonic, lexicographically sortable id. Audit events and tool calls written
    in one transaction can share a timestamp; sorting by (timestamp, id) keeps their true order."""
    global _last_ordered
    with _order_lock:
        _last_ordered = max(_last_ordered + 1, time.time_ns())
        return f"{_last_ordered:020d}-{uuid.uuid4().hex[:8]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(enum_cls: type[Any]) -> Enum:
    # Stored as VARCHAR (no native enum); unknown strings are rejected on write.
    return Enum(enum_cls, native_enum=False, validate_strings=True, length=32)


def _id_column() -> Mapped[str]:
    return mapped_column(primary_key=True, default=new_id)


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[str] = _id_column()
    name: Mapped[str] = mapped_column(unique=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # Compliance history is never deleted as a side effect: passive_deletes="all" stops the ORM
    # from nulling or deleting children, so the RESTRICT foreign keys reject the delete.
    documents: Mapped[list["Document"]] = relationship(
        back_populates="vendor", passive_deletes="all"
    )
    reviews: Mapped[list["ComplianceReview"]] = relationship(
        back_populates="vendor", passive_deletes="all"
    )


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = _id_column()
    name: Mapped[str]
    version: Mapped[str]
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = _id_column()
    vendor_id: Mapped[str] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), index=True
    )
    document_type: Mapped[str]
    filename: Mapped[str]
    source: Mapped[str]
    content: Mapped[str] = mapped_column(Text, default="")
    extraction_method: Mapped[str | None] = mapped_column(default=None)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    vendor: Mapped[Vendor] = relationship(back_populates="documents")
    evidence_checks: Mapped[list["EvidenceCheck"]] = relationship(
        back_populates="document", passive_deletes="all"
    )


class ComplianceReview(Base):
    __tablename__ = "compliance_reviews"

    id: Mapped[str] = _id_column()
    vendor_id: Mapped[str] = mapped_column(
        ForeignKey("vendors.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        _enum(WorkflowStatus), default=WorkflowStatus.RECEIVED
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    vendor: Mapped[Vendor] = relationship(back_populates="reviews")
    evidence_checks: Mapped[list["EvidenceCheck"]] = relationship(
        back_populates="review", passive_deletes="all"
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="review", passive_deletes="all"
    )


class EvidenceCheck(Base):
    __tablename__ = "evidence_checks"

    id: Mapped[str] = _id_column()
    review_id: Mapped[str] = mapped_column(
        ForeignKey("compliance_reviews.id", ondelete="RESTRICT"), index=True
    )
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), default=None
    )
    requirement: Mapped[str]
    status: Mapped[EvidenceStatus] = mapped_column(
        _enum(EvidenceStatus), default=EvidenceStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    review: Mapped[ComplianceReview] = relationship(back_populates="evidence_checks")
    document: Mapped[Document | None] = relationship(back_populates="evidence_checks")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = _id_column()
    review_id: Mapped[str] = mapped_column(
        ForeignKey("compliance_reviews.id", ondelete="RESTRICT"), index=True
    )
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    decision: Mapped[ApprovalDecision] = mapped_column(
        _enum(ApprovalDecision), default=ApprovalDecision.PENDING
    )
    # `approver` is the approver's ROLE (the policy engine compares it to the officer role).
    approver: Mapped[str | None] = mapped_column(default=None)
    decided_by: Mapped[str | None] = mapped_column(default=None)  # the acting person/account
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    run_id: Mapped[str | None] = mapped_column(default=None, index=True)  # run that requested it

    review: Mapped[ComplianceReview] = relationship(back_populates="approvals")

    __table_args__ = (
        # At most one open request per review, even under concurrent runs.
        Index(
            "uq_one_pending_approval_per_review",
            "review_id",
            unique=True,
            sqlite_where=text("decision = 'PENDING'"),
        ),
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = _id_column()
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[WorkflowStatus] = mapped_column(
        _enum(WorkflowStatus), default=WorkflowStatus.RECEIVED
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # Persisted result of the run (set by the runtime / approval resume, never by the model).
    outcome: Mapped[str | None] = mapped_column(default=None)
    vendor_id: Mapped[str | None] = mapped_column(default=None, index=True)
    review_id: Mapped[str | None] = mapped_column(default=None)
    approval_id: Mapped[str | None] = mapped_column(default=None, index=True)
    summary: Mapped[str | None] = mapped_column(Text, default=None)  # built from verified data
    model_summary: Mapped[str | None] = mapped_column(Text, default=None)  # untrusted model text

    tool_calls: Mapped[list["ToolCall"]] = relationship(
        back_populates="run", passive_deletes="all", order_by="(ToolCall.timestamp, ToolCall.id)"
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="run",
        passive_deletes="all",
        order_by="(AuditEvent.timestamp, AuditEvent.id)",
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_ordered_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), index=True
    )
    tool_name: Mapped[str]
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    status: Mapped[ToolCallStatus] = mapped_column(_enum(ToolCallStatus))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    run: Mapped[AgentRun] = relationship(back_populates="tool_calls")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_ordered_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), index=True
    )
    event_type: Mapped[str]
    actor: Mapped[str]
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    run: Mapped[AgentRun] = relationship(back_populates="audit_events")
