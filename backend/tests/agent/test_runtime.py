from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import LLMError, LLMResponse, ToolUse
from app.agent.runtime import AgentLimits, AgentRuntime, RunOutcome
from app.domain.enums import ApprovalDecision, ToolCallStatus, WorkflowStatus
from app.persistence.models import (
    AgentRun,
    Approval,
    AuditEvent,
    ComplianceReview,
    Document,
    ToolCall,
    Vendor,
)

GOAL = "Review Acme Corp's compliance status and prepare it for approval."


class ScriptedLLM:
    """Replays canned responses; an Exception entry is raised instead of returned."""

    def __init__(self, *script: LLMResponse | Exception) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(name: str, args: object, id_: str = "tu_1") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        text="",
        tool_uses=[ToolUse(id=id_, name=name, input=args)],
        content=[{"type": "tool_use", "id": id_, "name": name, "input": args}],
    )


def done(text: str = "All good, vendor is compliant.") -> LLMResponse:
    return LLMResponse(stop_reason="end_turn", text=text, content=[{"type": "text", "text": text}])


@pytest.fixture
def vendor_id(session: Session) -> str:
    vendor = Vendor(name="Acme Corp")
    session.add(vendor)
    session.commit()
    return vendor.id


def add_valid_evidence(session: Session, vendor_id: str) -> None:
    recent = (datetime.now(UTC) - timedelta(days=60)).date().isoformat()
    for doc_type, facts in [
        ("soc2_report", {"report_type": "type_ii", "period_end": recent}),
        ("dpa", {"signed": True}),
        ("security_questionnaire", {"questions_total": 10, "questions_answered": 10}),
    ]:
        session.add(
            Document(
                vendor_id=vendor_id,
                document_type=doc_type,
                filename=f"{doc_type}.pdf",
                source="test",
                metadata_=facts,
            )
        )
    session.commit()


def make_runtime(factory: sessionmaker[Session], llm: ScriptedLLM, **kw: Any) -> AgentRuntime:
    return AgentRuntime(llm, factory, **kw)


def review_id_of(session: Session, vendor_id: str) -> str:
    return session.scalars(
        select(ComplianceReview.id).where(ComplianceReview.vendor_id == vendor_id)
    ).one()


def test_valid_evidence_without_approval_waits_and_creates_pending_request(
    session_factory: sessionmaker[Session], session: Session, vendor_id: str
) -> None:
    add_valid_evidence(session, vendor_id)
    llm = ScriptedLLM(done())  # model claims compliance without calling any tool
    result = make_runtime(session_factory, llm).run(GOAL)

    assert result.status is WorkflowStatus.WAITING_APPROVAL
    assert result.outcome is RunOutcome.AWAITING_APPROVAL
    assert result.human_action_required
    assert result.approval_id is not None
    with session_factory() as s:
        approval = s.get(Approval, result.approval_id)
        assert approval is not None
        assert approval.decision is ApprovalDecision.PENDING
        assert approval.approver is None
        assert s.get(AgentRun, result.run_id).status is WorkflowStatus.WAITING_APPROVAL  # type: ignore[union-attr]


def test_model_cannot_complete_run_by_claiming_compliance(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    # No evidence at all, but the model insists everything is fine.
    result = make_runtime(session_factory, ScriptedLLM(done("Fully compliant, approved!"))).run(
        GOAL
    )
    assert result.status is WorkflowStatus.FAILED
    assert result.outcome is RunOutcome.EVIDENCE_DEFICIENT
    assert result.model_summary == "Fully compliant, approved!"
    assert "Not compliant" in result.summary


def test_completed_only_with_valid_evidence_and_human_officer_approval(
    session_factory: sessionmaker[Session], session: Session, vendor_id: str
) -> None:
    add_valid_evidence(session, vendor_id)
    review = ComplianceReview(vendor_id=vendor_id)
    session.add(review)
    session.flush()
    session.add(
        Approval(
            review_id=review.id, decision=ApprovalDecision.APPROVED, approver="compliance_officer"
        )
    )
    session.commit()

    result = make_runtime(session_factory, ScriptedLLM(done())).run(GOAL)
    assert result.status is WorkflowStatus.COMPLETED
    assert result.outcome is RunOutcome.COMPLIANT_APPROVED
    assert not result.human_action_required


def test_rejected_approval_fails_run(
    session_factory: sessionmaker[Session], session: Session, vendor_id: str
) -> None:
    add_valid_evidence(session, vendor_id)
    review = ComplianceReview(vendor_id=vendor_id)
    session.add(review)
    session.flush()
    session.add(
        Approval(
            review_id=review.id, decision=ApprovalDecision.REJECTED, approver="compliance_officer"
        )
    )
    session.commit()
    result = make_runtime(session_factory, ScriptedLLM(done())).run(GOAL)
    assert result.status is WorkflowStatus.FAILED
    assert result.outcome is RunOutcome.APPROVAL_REJECTED


def test_tool_calls_go_through_runtime_and_are_persisted(
    session_factory: sessionmaker[Session], session: Session, vendor_id: str
) -> None:
    add_valid_evidence(session, vendor_id)
    review = ComplianceReview(vendor_id=vendor_id)
    session.add(review)
    session.commit()
    llm = ScriptedLLM(
        tool_call("evaluate_vendor_compliance", {"review_id": review.id}, "a"),
        tool_call("get_vendor", {"name": "Acme Corp"}, "b"),
        done(),
    )
    result = make_runtime(session_factory, llm).run(GOAL)

    assert result.tool_calls == 2 and result.failed_tool_calls == 0
    with session_factory() as s:
        calls = list(s.scalars(select(ToolCall).where(ToolCall.run_id == result.run_id)))
        assert [c.tool_name for c in calls] == ["evaluate_vendor_compliance", "get_vendor"]
        assert all(c.status is ToolCallStatus.SUCCESS for c in calls)
    # The tool result was fed back to the model as a tool_result block.
    last_user = llm.requests[1]["messages"][-1]
    assert last_user["content"][0]["type"] == "tool_result"
    assert last_user["content"][0]["tool_use_id"] == "a"
    assert "is_error" not in last_user["content"][0]


@pytest.mark.parametrize(
    ("name", "args", "code"),
    [
        ("drop_tables", {}, "UNKNOWN_TOOL"),
        ("get_vendor", {"name": "x", "bogus": 1}, "INVALID_ARGUMENTS"),
        ("get_vendor", "not-a-dict", "INVALID_ARGUMENTS"),
        ("get_vendor", {"name": "Nobody Inc"}, "NOT_FOUND"),
    ],
)
def test_failed_tool_calls_are_recorded_as_failed_and_reported_as_errors(
    session_factory: sessionmaker[Session], vendor_id: str, name: str, args: object, code: str
) -> None:
    llm = ScriptedLLM(tool_call(name, args), done())
    result = make_runtime(session_factory, llm).run(GOAL)

    assert result.failed_tool_calls == 1
    block = llm.requests[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True
    assert code in block["content"]
    with session_factory() as s:
        call = s.scalars(select(ToolCall).where(ToolCall.run_id == result.run_id)).one()
        assert call.status is ToolCallStatus.FAILED
        assert call.output is not None and call.output["error"]["code"] == code


def test_agent_principal_without_permission_is_denied(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    from app.tools.permissions import Permission, Principal

    limited = Principal("limited", frozenset({Permission.VENDOR_READ}))
    llm = ScriptedLLM(tool_call("evaluate_vendor_compliance", {"review_id": "x"}), done())
    result = make_runtime(session_factory, llm, principal=limited).run(GOAL)
    assert result.failed_tool_calls == 1
    assert "PERMISSION_DENIED" in llm.requests[1]["messages"][-1]["content"][0]["content"]


def test_iteration_limit_fails_run(session_factory: sessionmaker[Session], vendor_id: str) -> None:
    llm = ScriptedLLM(*[tool_call("get_vendor", {"name": "Acme Corp"}, f"t{i}") for i in range(5)])
    result = make_runtime(session_factory, llm, limits=AgentLimits(max_iterations=3)).run(GOAL)
    assert result.status is WorkflowStatus.FAILED
    assert result.outcome is RunOutcome.LIMIT_REACHED
    assert result.iterations == 3


def test_tool_call_limit_fails_run_and_answers_every_tool_use(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    many = LLMResponse(
        stop_reason="tool_use",
        text="",
        tool_uses=[ToolUse(f"t{i}", "get_vendor", {"name": "Acme Corp"}) for i in range(4)],
    )
    llm = ScriptedLLM(many)
    result = make_runtime(session_factory, llm, limits=AgentLimits(max_tool_calls=2)).run(GOAL)
    assert result.outcome is RunOutcome.LIMIT_REACHED
    assert result.tool_calls == 2


def test_api_error_fails_run_and_is_audited(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    result = make_runtime(session_factory, ScriptedLLM(LLMError("boom"))).run(GOAL)
    assert result.status is WorkflowStatus.FAILED
    assert result.outcome is RunOutcome.MODEL_ERROR
    with session_factory() as s:
        types = [e.event_type for e in s.scalars(select(AuditEvent))]
        assert "llm_error" in types


def test_truncated_model_response_is_not_a_clean_stop(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    llm = ScriptedLLM(LLMResponse(stop_reason="max_tokens", text="partial"))
    result = make_runtime(session_factory, llm).run(GOAL)
    assert result.status is WorkflowStatus.FAILED
    assert result.outcome is RunOutcome.MODEL_ERROR


@pytest.mark.parametrize("goal", ["Review compliance of nobody", "Compare Acme Corp and Beta Ltd"])
def test_goal_must_identify_exactly_one_vendor(
    session_factory: sessionmaker[Session], session: Session, vendor_id: str, goal: str
) -> None:
    session.add(Vendor(name="Beta Ltd"))
    session.commit()
    llm = ScriptedLLM()
    result = make_runtime(session_factory, llm).run(goal)
    assert result.outcome is RunOutcome.INVALID_TARGET
    assert result.status is WorkflowStatus.FAILED
    assert llm.requests == []  # the model was never consulted


def test_audit_trail_and_lifecycle(
    session_factory: sessionmaker[Session], session: Session, vendor_id: str
) -> None:
    add_valid_evidence(session, vendor_id)
    result = make_runtime(session_factory, ScriptedLLM(done())).run(GOAL)
    with session_factory() as s:
        events = list(s.scalars(select(AuditEvent).where(AuditEvent.run_id == result.run_id)))
        transitions = [e.details["to"] for e in events if e.event_type == "status_changed"]
        assert transitions == ["PLANNING", "EXECUTING", "VERIFYING", "WAITING_APPROVAL"]
        assert {"run_created", "context_loaded", "verification", "approval_pending"} <= {
            e.event_type for e in events
        }
        run = s.get(AgentRun, result.run_id)
        assert run is not None and run.completed_at is None  # waiting, not terminal
