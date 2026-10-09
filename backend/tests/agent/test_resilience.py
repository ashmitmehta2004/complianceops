"""Bounded recovery, invalid model output, state-machine and schema-evolution invariants."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import LLMError
from app.agent.runtime import AgentLimits, AgentRuntime, RunOutcome
from app.agent.workflow import InvalidTransition, transition_run
from app.domain.enums import ToolCallStatus, WorkflowStatus
from app.persistence.database import init_db
from app.persistence.migrations import upgrade_schema
from app.persistence.models import AgentRun, AuditEvent, ToolCall, Vendor
from app.tools.permissions import Permission
from app.tools.readonly import build_default_registry
from tests.agent.test_runtime import GOAL, ScriptedLLM, done, tool_call

RUNS_DDL = (
    "CREATE TABLE agent_runs (id VARCHAR PRIMARY KEY, goal TEXT, status VARCHAR(32), "
    "started_at DATETIME, completed_at DATETIME)"
)
APPROVALS_DDL = (
    "CREATE TABLE approvals (id VARCHAR PRIMARY KEY, review_id VARCHAR, requested_at DATETIME, "
    "resolved_at DATETIME, decision VARCHAR(32), approver VARCHAR)"
)
FAST = AgentLimits(retry_backoff_seconds=0)


@pytest.fixture
def vendor_id(session: Session) -> str:
    vendor = Vendor(name="Acme Corp")
    session.add(vendor)
    session.commit()
    return vendor.id


def event_types(factory: sessionmaker[Session], run_id: str) -> list[str]:
    with factory() as s:
        events = (
            s.query(AuditEvent)
            .filter_by(run_id=run_id)
            .order_by(AuditEvent.timestamp, AuditEvent.id)
        )
        return [e.event_type for e in events]


def test_transient_provider_error_is_retried_then_succeeds(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    llm = ScriptedLLM(LLMError("ServerError (HTTP 503)", retryable=True), done())
    result = AgentRuntime(llm, session_factory, limits=FAST).run(GOAL)
    assert result.outcome is RunOutcome.EVIDENCE_DEFICIENT  # ran to verification, not MODEL_ERROR
    assert "llm_retry" in event_types(session_factory, result.run_id)


def test_retries_are_bounded(session_factory: sessionmaker[Session], vendor_id: str) -> None:
    err = LLMError("ServerError (HTTP 503)", retryable=True)
    llm = ScriptedLLM(err, err, err, err, err)
    result = AgentRuntime(llm, session_factory, limits=FAST).run(GOAL)
    assert result.outcome is RunOutcome.MODEL_ERROR
    assert len(llm.requests) == 3  # 1 attempt + max_llm_retries (2)


def test_non_retryable_provider_error_is_not_retried(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    llm = ScriptedLLM(LLMError("ClientError (HTTP 401)"), done())
    result = AgentRuntime(llm, session_factory, limits=FAST).run(GOAL)
    assert result.outcome is RunOutcome.MODEL_ERROR and len(llm.requests) == 1


def test_model_recovers_from_failed_tool_call_and_verdict_stays_deterministic(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    llm = ScriptedLLM(
        tool_call("evaluate_vendor_compliance", {"review_id": "does-not-exist"}, "t1"),
        done("Retried and everything is fine."),
    )
    result = AgentRuntime(llm, session_factory, limits=FAST).run(GOAL)
    assert result.failed_tool_calls == 1
    with session_factory() as s:
        call = s.query(ToolCall).one()
        assert call.status is ToolCallStatus.FAILED
        assert call.output is not None and call.output["error"]["code"] == "NOT_FOUND"
    assert result.outcome is RunOutcome.EVIDENCE_DEFICIENT  # model optimism ignored


def test_malformed_tool_arguments_and_unknown_tools_are_structured_errors(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    llm = ScriptedLLM(
        tool_call("evaluate_vendor_compliance", "not-an-object", "t1"),
        tool_call("approve_vendor", {"vendor_id": "x"}, "t2"),
        done(),
    )
    result = AgentRuntime(llm, session_factory, limits=FAST).run(GOAL)
    with session_factory() as s:
        codes = [c.output["error"]["code"] for c in s.query(ToolCall).order_by(ToolCall.id)]  # type: ignore[index]
    assert codes == ["INVALID_ARGUMENTS", "UNKNOWN_TOOL"]
    assert result.status is WorkflowStatus.FAILED


def test_agent_has_no_write_or_approval_capability() -> None:
    names = {t.name for t in build_default_registry().list()}
    assert not any("approv" in n or "decide" in n or "reject" in n for n in names)
    assert all(
        t.required_permission.value.endswith((":read", ":evaluate"))
        for t in build_default_registry().list()
    )
    assert {p.value for p in Permission} <= {
        "vendor:read",
        "policy:read",
        "evidence:read",
        "document:read",
        "compliance:evaluate",
    }


def test_run_context_and_result_are_persisted_on_the_run(
    session_factory: sessionmaker[Session], vendor_id: str
) -> None:
    result = AgentRuntime(ScriptedLLM(done("hi")), session_factory, limits=FAST).run(GOAL)
    with session_factory() as s:
        run = s.get(AgentRun, result.run_id)
        assert run is not None
        assert run.outcome == "EVIDENCE_DEFICIENT" and run.vendor_id == vendor_id
        assert run.review_id == result.review_id and run.model_summary == "hi"
        assert run.completed_at is not None and run.summary


def test_invalid_state_transitions_are_rejected(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    run = AgentRun(goal="x")
    session.add(run)
    session.flush()
    with pytest.raises(InvalidTransition):
        transition_run(session, run, WorkflowStatus.COMPLETED)  # cannot skip verification
    run.status = WorkflowStatus.COMPLETED
    with pytest.raises(InvalidTransition):
        transition_run(session, run, WorkflowStatus.EXECUTING)  # terminal is final


def test_upgrade_adds_missing_columns_to_an_old_database_and_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:  # the original schema: no outcome/decided_by/... columns
        conn.execute(text(RUNS_DDL))
        conn.execute(text(APPROVALS_DDL))
        conn.execute(text("INSERT INTO agent_runs (id, goal, status) VALUES ('r1', 'g', 'FAILED')"))
    applied = upgrade_schema(engine)
    assert "agent_runs.outcome" in applied and "approvals.decided_by" in applied
    cols = {c["name"] for c in inspect(engine).get_columns("agent_runs")}
    assert {"outcome", "vendor_id", "review_id", "approval_id", "summary", "model_summary"} <= cols
    assert upgrade_schema(engine) == []  # idempotent
    init_db(engine)  # full init on top of an upgraded legacy db works
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM agent_runs")).scalar() == "r1"  # data preserved
