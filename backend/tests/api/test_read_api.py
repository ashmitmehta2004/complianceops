"""Read endpoints: vendors, runs listing, readiness, metadata, error shapes, frontend hosting."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import LLMError, LLMResponse
from app.main import create_app
from app.persistence.models import AgentRun
from app.seed import seed


class FinishLLM:
    def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        return LLMResponse(stop_reason="end_turn", text="done")


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> TestClient:
    seed(session_factory)
    return TestClient(create_app(session_factory, FinishLLM, demo_actor="demo"))


def vendors_by_name(client: TestClient) -> dict[str, Any]:
    return {v["name"]: v for v in client.get("/vendors").json()}


def test_vendor_list_reports_live_deterministic_status(client: TestClient) -> None:
    vendors = vendors_by_name(client)
    assert set(vendors) == {"Acme Corp", "Globex Industries", "Initech", "Hooli"}
    assert vendors["Acme Corp"]["evidence_compliant"] is True
    assert vendors["Acme Corp"]["approval_satisfied"] is False
    assert vendors["Globex Industries"]["evidence_compliant"] is False
    assert vendors["Initech"]["evidence_compliant"] is False
    assert vendors["Hooli"]["evidence_compliant"] and vendors["Hooli"]["approval_satisfied"]
    assert vendors["Acme Corp"]["last_run"] is None


def test_globex_requirements_explain_each_deficiency(client: TestClient) -> None:
    detail = client.get(f"/vendors/{vendors_by_name(client)['Globex Industries']['id']}").json()
    reqs = {r["requirement"]: r for r in detail["requirements"]}
    assert reqs["SOC2_TYPE_II"]["status"] == "EXPIRED"
    assert reqs["SOC2_TYPE_II"]["reason"] == "REPORT_OLDER_THAN_MAX_AGE"
    assert "12 months" in reqs["SOC2_TYPE_II"]["explanation"]
    assert reqs["DPA"]["reason"] == "NOT_SIGNED"
    assert reqs["SECURITY_QUESTIONNAIRE"]["reason"] == "QUESTIONS_UNANSWERED"
    assert {d["document_type"] for d in detail["documents"]} == {
        "soc2_report",
        "dpa",
        "security_questionnaire",
    }


def test_initech_has_no_evidence_all_missing(client: TestClient) -> None:
    detail = client.get(f"/vendors/{vendors_by_name(client)['Initech']['id']}").json()
    assert detail["documents"] == []
    statuses = {r["requirement"]: r["status"] for r in detail["requirements"]}
    assert statuses["SOC2_TYPE_II"] == statuses["DPA"] == "MISSING"


def test_hooli_seeded_approval_is_labelled_as_fixture(client: TestClient) -> None:
    detail = client.get(f"/vendors/{vendors_by_name(client)['Hooli']['id']}").json()
    approval = detail["reviews"][0]["approvals"][0]
    assert approval["decision"] == "APPROVED" and approval["is_fixture"] is True
    run = client.post("/runs", json={"vendor_name": "Hooli"}).json()
    assert run["status"] == "COMPLETED" and run["outcome"] == "COMPLIANT_APPROVED"


def test_run_listing_filters_and_detail_persistence(client: TestClient) -> None:
    acme = client.post("/runs", json={"vendor_name": "Acme Corp"}).json()
    globex = client.post("/runs", json={"vendor_name": "Globex Industries"}).json()
    listed = client.get("/runs").json()
    assert [r["run_id"] for r in listed] == [globex["run_id"], acme["run_id"]]  # newest first
    assert listed[0]["vendor_name"] == "Globex Industries"
    assert listed[0]["outcome"] == "EVIDENCE_DEFICIENT"
    waiting = client.get("/runs", params={"status": "WAITING_APPROVAL"}).json()
    assert [r["run_id"] for r in waiting] == [acme["run_id"]]
    by_vendor = client.get("/runs", params={"vendor_id": globex["vendor_id"]}).json()
    assert [r["run_id"] for r in by_vendor] == [globex["run_id"]]
    assert len(client.get("/runs", params={"limit": 1}).json()) == 1
    assert client.get("/runs", params={"status": "BOGUS"}).status_code == 422
    assert client.get("/runs", params={"limit": 0}).status_code == 422
    # Detail includes persisted audit history, ordered, with verified requirement results.
    detail = client.get(f"/runs/{globex['run_id']}").json()
    types = [e["event_type"] for e in detail["audit_events"]]
    assert types[0] == "run_created" and types[-1] == "status_changed"
    assert "context_loaded" in types and "verification" in types and "policy_loaded" in types
    assert {r["status"] for r in detail["requirements"]} >= {"EXPIRED", "INCOMPLETE", "MISSING"}


def test_model_text_is_kept_separate_from_verified_facts(client: TestClient) -> None:
    run = client.post("/runs", json={"vendor_name": "Globex Industries"}).json()
    assert run["model_summary"] == "done"
    assert run["outcome"] == "EVIDENCE_DEFICIENT"
    assert run["summary"] != run["model_summary"]


def test_health_ready_and_meta(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "database": "ok", "llm_configured": True}
    meta = client.get("/meta").json()
    assert meta["demo_actor"] == "demo" and meta["demo_actor_is_authentication"] is False


def test_ready_reports_unconfigured_llm_without_secrets(
    session_factory: sessionmaker[Session],
) -> None:
    def broken() -> FinishLLM:
        raise LLMError("GEMINI_API_KEY is not set")

    body = TestClient(create_app(session_factory, broken)).get("/ready").json()
    assert body["llm_configured"] is False and "GEMINI" not in str(body)


def test_error_shape_is_consistent(client: TestClient) -> None:
    for response in (
        client.get("/runs/missing"),
        client.get("/vendors/missing"),
        client.get("/approvals/missing"),
        client.get("/no/such/route"),
        client.post("/runs", json={"vendor_name": "Nobody Inc"}),
    ):
        body = response.json()
        assert response.status_code == 404 and set(body) == {"detail", "code"}
    bad = client.post("/runs", json={"vendor_name": ""})
    assert bad.status_code == 422 and bad.json()["code"] == "validation_error"
    assert bad.json()["errors"][0]["field"] == "vendor_name"


def test_interrupted_runs_are_failed_on_startup_but_waiting_runs_are_kept(
    session_factory: sessionmaker[Session],
) -> None:
    seed(session_factory)
    with session_factory() as s:
        stuck = AgentRun(goal="Review Acme Corp", status="EXECUTING")
        waiting = AgentRun(goal="Review Acme Corp", status="WAITING_APPROVAL")
        s.add_all([stuck, waiting])
        s.commit()
        stuck_id, waiting_id = stuck.id, waiting.id
    with TestClient(create_app(session_factory, FinishLLM)) as client:  # runs lifespan
        a = client.get(f"/runs/{stuck_id}").json()
        assert a["status"] == "FAILED" and a["outcome"] == "INTERRUPTED"
        assert a["completed_at"] is not None
        assert client.get(f"/runs/{waiting_id}").json()["status"] == "WAITING_APPROVAL"


def test_frontend_is_served_by_the_backend(client: TestClient) -> None:
    index = client.get("/app/")
    assert index.status_code == 200 and "ComplianceOps" in index.text
    assert client.get("/app/js/main.js").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/app/"


def test_seeded_hooli_review_is_completed_and_seed_stays_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    from sqlalchemy import func, select

    from app.domain.enums import WorkflowStatus
    from app.persistence.models import Approval, ComplianceReview, Vendor

    seed(session_factory)
    again = seed(session_factory)
    assert again["reviews"] == 0 and again["approvals"] == 0
    with session_factory() as s:
        hooli = s.scalars(select(Vendor).where(Vendor.name == "Hooli")).one()
        reviews = s.scalars(
            select(ComplianceReview).where(ComplianceReview.vendor_id == hooli.id)
        ).all()
        assert [r.status for r in reviews] == [WorkflowStatus.COMPLETED]
        assert s.scalar(select(func.count()).select_from(Approval)) == 1
    client = TestClient(create_app(session_factory, FinishLLM))
    summary = next(v for v in client.get("/vendors").json() if v["name"] == "Hooli")
    assert summary["review_status"] == "COMPLETED"
    assert summary["evidence_compliant"] is True and summary["approval_satisfied"] is True


def test_seed_repairs_a_legacy_hooli_review_left_in_received(
    session_factory: sessionmaker[Session],
) -> None:
    from sqlalchemy import select

    from app.domain.enums import WorkflowStatus
    from app.persistence.models import ComplianceReview

    seed(session_factory)
    with session_factory() as s:
        review = s.scalars(select(ComplianceReview)).one()
        review.status = WorkflowStatus.RECEIVED
        s.commit()
    seed(session_factory)
    with session_factory() as s:
        assert s.scalars(select(ComplianceReview)).one().status is WorkflowStatus.COMPLETED
