from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.domain.enums import ApprovalDecision, EvidenceStatus, ToolCallStatus, WorkflowStatus
from app.persistence.database import create_db_engine, get_database_url, init_db
from app.persistence.models import (
    AgentRun,
    Approval,
    AuditEvent,
    ComplianceReview,
    Document,
    EvidenceCheck,
    Policy,
    ToolCall,
    Vendor,
)


def make_vendor(session: Session, name: str = "Acme Corp") -> Vendor:
    vendor = Vendor(name=name, metadata_={"country": "DE"})
    session.add(vendor)
    session.commit()
    return vendor


def make_document(vendor: Vendor) -> Document:
    return Document(
        vendor=vendor,
        document_type="insurance_certificate",
        filename="cert.pdf",
        source="upload",
        content="Policy valid until 2027-01-01",
        extraction_method="text",
        metadata_={"pages": 1},
    )


def reload[T](session: Session, model: type[T], pk: str) -> T:
    session.expire_all()
    loaded = session.get(model, pk)
    assert loaded is not None
    return loaded


# 1. initialization
def test_init_db_creates_all_tables(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) == {
        "vendors",
        "policies",
        "documents",
        "compliance_reviews",
        "evidence_checks",
        "approvals",
        "agent_runs",
        "tool_calls",
        "audit_events",
    }


def test_init_db_is_idempotent(engine: Engine) -> None:
    init_db(engine)
    assert len(inspect(engine).get_table_names()) == 9


def test_database_url_comes_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = f"sqlite:///{tmp_path / 'env.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    assert get_database_url() == url
    assert create_db_engine().url.database == str(tmp_path / "env.db")


# 2. vendor
def test_create_and_retrieve_vendor(session: Session) -> None:
    vendor = make_vendor(session)

    loaded = reload(session, Vendor, vendor.id)

    assert loaded.name == "Acme Corp"
    assert loaded.metadata_ == {"country": "DE"}
    assert len(loaded.id) == 36
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at.utcoffset() == timedelta(0)


def test_ids_are_unique(session: Session) -> None:
    a, b = make_vendor(session, "A"), make_vendor(session, "B")
    assert a.id != b.id


def test_naive_datetime_is_rejected(session: Session) -> None:
    session.add(Vendor(name="Naive", created_at=datetime(2025, 1, 1)))  # noqa: DTZ001
    with pytest.raises(StatementError):
        session.commit()


def test_policy_rules_roundtrip_as_json(session: Session) -> None:
    rules = {"required": ["insurance", "tax_id"], "min_coverage": 1_000_000}
    session.add(Policy(name="Vendor Onboarding", version="1.0", rules=rules))
    session.commit()
    session.expire_all()

    assert session.scalars(select(Policy)).one().rules == rules


# 3. review
def test_create_review_for_vendor(session: Session) -> None:
    vendor = make_vendor(session)
    review = ComplianceReview(vendor=vendor)
    session.add(review)
    session.commit()

    loaded = reload(session, ComplianceReview, review.id)
    assert loaded.vendor_id == vendor.id
    assert loaded.status is WorkflowStatus.RECEIVED
    assert loaded.updated_at.tzinfo is not None


def test_review_updated_at_changes_on_update(session: Session) -> None:
    first = datetime.now(UTC) - timedelta(hours=1)
    review = ComplianceReview(vendor=make_vendor(session), updated_at=first)
    session.add(review)
    session.commit()

    review.status = WorkflowStatus.PLANNING
    session.commit()

    assert review.updated_at > first


def test_review_requires_existing_vendor(session: Session) -> None:
    session.add(ComplianceReview(vendor_id="does-not-exist"))
    with pytest.raises(IntegrityError):
        session.commit()


# 4. evidence
def test_evidence_check_links_review_and_document(session: Session) -> None:
    vendor = make_vendor(session)
    document = make_document(vendor)
    review = ComplianceReview(vendor=vendor)
    check = EvidenceCheck(
        review=review,
        document=document,
        requirement="Valid insurance certificate",
        status=EvidenceStatus.VALID,
        reason="Expires 2027-01-01",
    )
    session.add_all([document, review, check])
    session.commit()

    loaded = reload(session, EvidenceCheck, check.id)
    assert loaded.review_id == review.id
    assert loaded.document_id == document.id
    assert loaded.status is EvidenceStatus.VALID
    assert loaded.reason == "Expires 2027-01-01"


def test_evidence_check_document_is_optional(session: Session) -> None:
    review = ComplianceReview(vendor=make_vendor(session))
    check = EvidenceCheck(
        review=review, requirement="Tax certificate", status=EvidenceStatus.MISSING
    )
    session.add(check)
    session.commit()

    loaded = reload(session, EvidenceCheck, check.id)
    assert loaded.document_id is None
    assert loaded.document is None


def test_evidence_check_defaults_to_pending(session: Session) -> None:
    check = EvidenceCheck(
        review=ComplianceReview(vendor=make_vendor(session)), requirement="Anything"
    )
    session.add(check)
    session.commit()
    assert reload(session, EvidenceCheck, check.id).status is EvidenceStatus.PENDING


# 5. approval
def test_approval_defaults_to_pending(session: Session) -> None:
    review = ComplianceReview(vendor=make_vendor(session), status=WorkflowStatus.WAITING_APPROVAL)
    approval = Approval(review=review)
    session.add(approval)
    session.commit()

    loaded = reload(session, Approval, approval.id)
    assert loaded.decision is ApprovalDecision.PENDING
    assert loaded.resolved_at is None
    assert loaded.approver is None
    assert loaded.requested_at.tzinfo is not None
    assert loaded.review.status is WorkflowStatus.WAITING_APPROVAL


def test_approval_can_be_resolved(session: Session) -> None:
    approval = Approval(review=ComplianceReview(vendor=make_vendor(session)))
    session.add(approval)
    session.commit()

    resolved = datetime.now(UTC)
    approval.decision = ApprovalDecision.APPROVED
    approval.approver = "alice"
    approval.resolved_at = resolved
    session.commit()

    loaded = reload(session, Approval, approval.id)
    assert loaded.decision is ApprovalDecision.APPROVED
    assert loaded.approver == "alice"
    assert loaded.resolved_at == resolved


# 6. agent run
def test_agent_run_with_tool_calls_and_audit_events(session: Session) -> None:
    # Explicit, clearly ordered timestamps: ordering must not depend on clock resolution.
    t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    run = AgentRun(goal="Review Acme Corp", status=WorkflowStatus.EXECUTING)
    run.tool_calls.append(
        ToolCall(
            tool_name="read_document",
            input={"document_id": "d1"},
            output={"text": "hello"},
            status=ToolCallStatus.SUCCESS,
            timestamp=t0,
        )
    )
    run.tool_calls.append(
        ToolCall(
            tool_name="check_policy",
            input={"x": 1},
            status=ToolCallStatus.FAILED,
            timestamp=t0 + timedelta(seconds=1),
        )
    )
    run.audit_events.append(
        AuditEvent(
            event_type="tool_called",
            actor="agent",
            details={"tool": "read_document"},
            timestamp=t0,
        )
    )
    session.add(run)
    session.commit()

    loaded = reload(session, AgentRun, run.id)
    assert loaded.completed_at is None
    assert [c.tool_name for c in loaded.tool_calls] == ["read_document", "check_policy"]
    assert loaded.tool_calls[0].input == {"document_id": "d1"}
    assert loaded.tool_calls[0].output == {"text": "hello"}
    assert loaded.tool_calls[1].status is ToolCallStatus.FAILED
    assert loaded.tool_calls[1].output is None
    assert loaded.audit_events[0].details == {"tool": "read_document"}
    assert loaded.audit_events[0].actor == "agent"


# 7. enums
@pytest.mark.parametrize("status", list(WorkflowStatus))
def test_every_workflow_status_persists_on_review_and_run(
    session: Session, status: WorkflowStatus
) -> None:
    review = ComplianceReview(vendor=make_vendor(session), status=status)
    run = AgentRun(goal="g", status=status)
    session.add_all([review, run])
    session.commit()

    assert reload(session, ComplianceReview, review.id).status is status
    assert reload(session, AgentRun, run.id).status is status


def test_workflow_status_contains_required_states() -> None:
    required = {
        "RECEIVED",
        "PLANNING",
        "EXECUTING",
        "WAITING_APPROVAL",
        "VERIFYING",
        "COMPLETED",
        "FAILED",
        "RETRYING",
    }
    assert required <= {s.value for s in WorkflowStatus}


def test_failed_is_distinct_from_completed(session: Session) -> None:
    assert WorkflowStatus.FAILED != WorkflowStatus.COMPLETED
    assert WorkflowStatus.FAILED.is_terminal
    assert WorkflowStatus.COMPLETED.is_terminal
    assert not WorkflowStatus.WAITING_APPROVAL.is_terminal

    vendor = make_vendor(session)
    done = ComplianceReview(vendor=vendor, status=WorkflowStatus.COMPLETED)
    failed = ComplianceReview(vendor=vendor, status=WorkflowStatus.FAILED)
    session.add_all([done, failed])
    session.commit()

    statuses = {r.id: r.status for r in session.scalars(select(ComplianceReview))}
    assert statuses[done.id] is WorkflowStatus.COMPLETED
    assert statuses[failed.id] is WorkflowStatus.FAILED


@pytest.mark.parametrize("decision", list(ApprovalDecision))
def test_every_approval_decision_persists(session: Session, decision: ApprovalDecision) -> None:
    approval = Approval(review=ComplianceReview(vendor=make_vendor(session)), decision=decision)
    session.add(approval)
    session.commit()
    assert reload(session, Approval, approval.id).decision is decision


@pytest.mark.parametrize("status", list(EvidenceStatus))
def test_every_evidence_status_persists(session: Session, status: EvidenceStatus) -> None:
    check = EvidenceCheck(
        review=ComplianceReview(vendor=make_vendor(session)), requirement="r", status=status
    )
    session.add(check)
    session.commit()
    assert reload(session, EvidenceCheck, check.id).status is status


@pytest.mark.parametrize("status", list(ToolCallStatus))
def test_every_tool_call_status_persists(session: Session, status: ToolCallStatus) -> None:
    call = ToolCall(run=AgentRun(goal="g"), tool_name="t", status=status)
    session.add(call)
    session.commit()
    assert reload(session, ToolCall, call.id).status is status


def test_invalid_status_string_is_rejected(session: Session) -> None:
    session.add(AgentRun(goal="g", status="BOGUS"))  # type: ignore[arg-type]
    with pytest.raises(StatementError):
        session.commit()


# 8. relationships in both directions
def test_relationships_navigate_in_both_directions(session: Session) -> None:
    vendor = make_vendor(session)
    document = make_document(vendor)
    review = ComplianceReview(vendor=vendor)
    check = EvidenceCheck(review=review, document=document, requirement="r")
    approval = Approval(review=review)
    session.add_all([document, review, check, approval])
    session.commit()

    loaded_vendor = reload(session, Vendor, vendor.id)
    assert [d.id for d in loaded_vendor.documents] == [document.id]
    assert [r.id for r in loaded_vendor.reviews] == [review.id]

    loaded_review = loaded_vendor.reviews[0]
    assert loaded_review.vendor.id == vendor.id
    assert [c.id for c in loaded_review.evidence_checks] == [check.id]
    assert [a.id for a in loaded_review.approvals] == [approval.id]

    loaded_check = loaded_review.evidence_checks[0]
    assert loaded_check.review.id == review.id
    assert loaded_check.document is not None
    assert loaded_check.document.vendor.id == vendor.id
    assert [c.id for c in loaded_check.document.evidence_checks] == [check.id]


def test_run_children_are_ordered_by_timestamp_not_insertion(session: Session) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    run = AgentRun(goal="g")
    # Appended newest-first, so only order_by can produce the expected order.
    for offset, name in [(2, "third"), (0, "first"), (1, "second")]:
        stamp = t0 + timedelta(seconds=offset)
        run.tool_calls.append(
            ToolCall(tool_name=name, status=ToolCallStatus.SUCCESS, timestamp=stamp)
        )
        run.audit_events.append(AuditEvent(event_type=name, actor="agent", timestamp=stamp))
    session.add(run)
    session.commit()

    loaded = reload(session, AgentRun, run.id)
    assert [c.tool_name for c in loaded.tool_calls] == ["first", "second", "third"]
    assert [e.event_type for e in loaded.audit_events] == ["first", "second", "third"]


# 9. audit-preserving deletes
def _delete_is_rejected(session: Session, obj: object) -> None:
    session.delete(obj)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_vendor_with_reviews_cannot_be_deleted(session: Session) -> None:
    vendor = make_vendor(session)
    review = ComplianceReview(vendor=vendor)
    check = EvidenceCheck(review=review, requirement="r")
    approval = Approval(review=review)
    session.add_all([review, check, approval])
    session.commit()

    _delete_is_rejected(session, vendor)

    assert session.get(Vendor, vendor.id) is not None
    assert session.get(ComplianceReview, review.id) is not None
    assert session.get(EvidenceCheck, check.id) is not None
    assert session.get(Approval, approval.id) is not None


def test_vendor_with_documents_cannot_be_deleted(session: Session) -> None:
    vendor = make_vendor(session)
    document = make_document(vendor)
    session.add(document)
    session.commit()

    _delete_is_rejected(session, vendor)

    assert session.get(Document, document.id) is not None


def test_document_referenced_by_evidence_cannot_be_deleted(session: Session) -> None:
    vendor = make_vendor(session)
    document = make_document(vendor)
    check = EvidenceCheck(
        review=ComplianceReview(vendor=vendor), document=document, requirement="r"
    )
    session.add_all([document, check])
    session.commit()

    _delete_is_rejected(session, document)

    assert session.get(Document, document.id) is not None
    assert reload(session, EvidenceCheck, check.id).document_id == document.id


def test_review_with_history_cannot_be_deleted(session: Session) -> None:
    review = ComplianceReview(vendor=make_vendor(session))
    check = EvidenceCheck(review=review, requirement="r")
    session.add_all([review, check])
    session.commit()

    _delete_is_rejected(session, review)

    assert session.get(EvidenceCheck, check.id) is not None


def test_run_with_tool_calls_and_audit_events_cannot_be_deleted(session: Session) -> None:
    run = AgentRun(goal="g")
    call = ToolCall(run=run, tool_name="t", status=ToolCallStatus.SUCCESS)
    event = AuditEvent(run=run, event_type="e", actor="agent")
    session.add_all([run, call, event])
    session.commit()

    _delete_is_rejected(session, run)

    assert session.get(AgentRun, run.id) is not None
    assert session.get(ToolCall, call.id) is not None
    assert session.get(AuditEvent, event.id) is not None


def test_run_with_only_tool_calls_cannot_be_deleted(session: Session) -> None:
    run = AgentRun(goal="g")
    call = ToolCall(run=run, tool_name="t", status=ToolCallStatus.SUCCESS)
    session.add_all([run, call])
    session.commit()

    _delete_is_rejected(session, run)

    assert session.get(ToolCall, call.id) is not None


def test_run_with_only_audit_events_cannot_be_deleted(session: Session) -> None:
    run = AgentRun(goal="g")
    event = AuditEvent(run=run, event_type="e", actor="agent")
    session.add_all([run, event])
    session.commit()

    _delete_is_rejected(session, run)

    assert session.get(AuditEvent, event.id) is not None


def test_run_without_history_can_be_deleted(session: Session) -> None:
    run = AgentRun(goal="g")
    session.add(run)
    session.commit()

    session.delete(run)
    session.commit()

    assert session.get(AgentRun, run.id) is None


def test_unreferenced_vendor_and_document_can_be_deleted(session: Session) -> None:
    vendor = make_vendor(session)
    document = make_document(vendor)
    session.add(document)
    session.commit()

    session.delete(document)
    session.commit()
    session.delete(vendor)
    session.commit()

    assert session.get(Vendor, vendor.id) is None


def test_run_children_point_back_to_run(session: Session) -> None:
    run = AgentRun(goal="g")
    call = ToolCall(run=run, tool_name="t", status=ToolCallStatus.SUCCESS)
    event = AuditEvent(run=run, event_type="e", actor="agent")
    session.add_all([run, call, event])
    session.commit()

    assert reload(session, ToolCall, call.id).run.id == run.id
    assert reload(session, AuditEvent, event.id).run.id == run.id
