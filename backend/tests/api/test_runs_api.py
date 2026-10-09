from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import LLMError, LLMResponse, ToolUse
from app.main import create_app
from app.persistence.models import Approval, Vendor
from app.seed import seed


class FakeLLM:
    def __init__(self, *script: LLMResponse) -> None:
        self._script = list(script)

    def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        return self._script.pop(0)


def finish(text: str = "Vendor is fully compliant and approved.") -> LLMResponse:
    return LLMResponse(stop_reason="end_turn", text=text)


def make_client(factory: sessionmaker[Session], *script: LLMResponse) -> TestClient:
    return TestClient(create_app(factory, lambda: FakeLLM(*script)))


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    seed(session_factory)
    return session_factory


def start(client: TestClient, vendor: str) -> dict[str, Any]:
    response = client.post("/runs", json={"vendor_name": vendor})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def test_valid_evidence_waits_for_approval_even_if_model_claims_success(
    seeded: sessionmaker[Session],
) -> None:
    body = start(make_client(seeded, finish()), "Acme Corp")

    assert body["status"] == "WAITING_APPROVAL"
    assert body["outcome"] == "AWAITING_APPROVAL"
    assert body["human_action_required"] is True
    assert body["approval_id"] is not None
    assert {
        r["status"]
        for r in body["requirements"]
        if r["requirement"] != "COMPLIANCE_OFFICER_APPROVAL"
    } == {"VALID"}
    with seeded() as s:
        approval = s.get(Approval, body["approval_id"])
        assert approval is not None and approval.decision.value == "PENDING"


def test_expired_and_missing_evidence_is_not_compliant(seeded: sessionmaker[Session]) -> None:
    expired = start(make_client(seeded, finish()), "Globex Industries")
    assert expired["status"] == "FAILED"
    assert expired["outcome"] == "EVIDENCE_DEFICIENT"
    by_req = {r["requirement"]: r for r in expired["requirements"]}
    assert by_req["SOC2_TYPE_II"]["status"] == "EXPIRED"
    assert by_req["DPA"]["reason"] == "NOT_SIGNED"

    missing = start(make_client(seeded, finish()), "initech")  # case-insensitive
    assert missing["outcome"] == "EVIDENCE_DEFICIENT"
    assert {r["status"] for r in missing["requirements"][:3]} == {"MISSING"}


def test_seeded_human_approval_completes_run(seeded: sessionmaker[Session]) -> None:
    body = start(make_client(seeded, finish()), "Hooli")
    assert body["status"] == "COMPLETED"
    assert body["human_action_required"] is False


def test_get_run_returns_persisted_state_and_audit_trail(seeded: sessionmaker[Session]) -> None:
    tool_use = LLMResponse(
        stop_reason="tool_use",
        text="",
        tool_uses=[ToolUse("t1", "get_vendor", {"name": "Acme Corp"})],
        content=[],
    )
    client = make_client(seeded, tool_use, finish())
    started = start(client, "Acme Corp")

    # A fresh app over the same database sees the same run: state is persisted, not in memory.
    fetched = TestClient(create_app(seeded, lambda: FakeLLM())).get(f"/runs/{started['run_id']}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == "WAITING_APPROVAL"
    assert body["summary"] == started["summary"]
    assert [c["tool_name"] for c in body["tool_calls"]] == ["get_vendor"]
    assert "verification" in {e["event_type"] for e in body["audit_events"]}


def test_unknown_vendor_and_run_return_404(seeded: sessionmaker[Session]) -> None:
    client = make_client(seeded)
    assert client.post("/runs", json={"vendor_name": "Nobody"}).status_code == 404
    assert client.get("/runs/does-not-exist").status_code == 404


@pytest.mark.parametrize("payload", [{}, {"vendor_name": ""}, {"vendor_name": "A", "x": 1}])
def test_invalid_request_body_is_422(
    seeded: sessionmaker[Session], payload: dict[str, Any]
) -> None:
    assert make_client(seeded).post("/runs", json=payload).status_code == 422


def test_missing_llm_configuration_is_503_without_details(seeded: sessionmaker[Session]) -> None:
    def broken() -> FakeLLM:
        raise LLMError("ANTHROPIC_API_KEY is not set")

    client = TestClient(create_app(seeded, broken))
    response = client.post("/runs", json={"vendor_name": "Acme Corp"})
    assert response.status_code == 503
    assert response.json() == {
        "detail": "LLM is not configured on the server",
        "code": "llm_not_configured",
    }


def test_model_api_failure_is_a_failed_run_without_leaking_exception_text(
    seeded: sessionmaker[Session],
) -> None:
    class Exploding:
        def create_message(self, **_: Any) -> LLMResponse:
            raise LLMError("APIStatusError (HTTP 500)")

    client = TestClient(create_app(seeded, lambda: Exploding()))
    body = start(client, "Acme Corp")
    assert body["status"] == "FAILED"
    assert body["outcome"] == "MODEL_ERROR"
    assert body["human_action_required"] is True


def test_health_and_docs_are_available(seeded: sessionmaker[Session]) -> None:
    client = make_client(seeded)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert "/runs" in client.get("/openapi.json").json()["paths"]


def test_seed_is_repeatable_and_creates_scenarios(session_factory: sessionmaker[Session]) -> None:
    first = seed(session_factory)
    second = seed(session_factory)

    assert first["vendors"] == 4 and first["documents"] > 0
    assert set(second.values()) == {0}
    with session_factory() as s:
        assert len(s.scalars(select(Vendor)).all()) == 4
