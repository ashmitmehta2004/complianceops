"""Human-in-the-loop lifecycle through the real HTTP API, runtime, policy engine and database."""

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import LLMResponse
from app.main import create_app
from app.persistence.models import AgentRun, Approval, AuditEvent, Document, Vendor
from app.seed import seed

ACTOR = "test.officer"


class FinishLLM:
    """A model that immediately claims success; the verifier must not care."""

    def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        return LLMResponse(stop_reason="end_turn", text="Vendor is fully compliant and approved.")


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    seed(session_factory)
    return session_factory


@pytest.fixture
def client(seeded: sessionmaker[Session]) -> TestClient:
    return TestClient(create_app(seeded, FinishLLM, demo_actor=ACTOR))


def start(client: TestClient, vendor: str) -> dict[str, Any]:
    response = client.post("/runs", json={"vendor_name": vendor})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def decide(client: TestClient, approval_id: str, decision: str, **extra: Any) -> Any:
    return client.post(f"/approvals/{approval_id}/decision", json={"decision": decision, **extra})


def events(factory: sessionmaker[Session], run_id: str) -> list[AuditEvent]:
    with factory() as s:
        return list(
            s.scalars(
                select(AuditEvent)
                .where(AuditEvent.run_id == run_id)
                .order_by(AuditEvent.timestamp, AuditEvent.id)
            )
        )


# ---- scenario A: valid evidence, approval required ----


def test_approve_resumes_waiting_run_to_completed(
    client: TestClient, seeded: sessionmaker[Session]
) -> None:
    run = start(client, "Acme Corp")
    assert run["status"] == "WAITING_APPROVAL"
    approval_id = run["approval_id"]

    pending = client.get("/approvals", params={"status": "PENDING"}).json()
    assert [a["id"] for a in pending] == [approval_id]
    view = pending[0]
    assert view["vendor_name"] == "Acme Corp" and view["can_approve"] is True
    assert view["run_ids"] == [run["run_id"]]

    response = decide(client, approval_id, "APPROVED", comment="  looks good ")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approval"]["decision"] == "APPROVED"
    assert body["approval"]["decided_by"] == ACTOR
    assert body["approval"]["role"] == "compliance_officer"
    assert body["approval"]["comment"] == "looks good"
    assert body["approval"]["is_fixture"] is False
    assert [r["run_id"] for r in body["resumed_runs"]] == [run["run_id"]]

    final = client.get(f"/runs/{run['run_id']}").json()
    assert final["status"] == "COMPLETED" and final["outcome"] == "COMPLIANT_APPROVED"
    assert final["completed_at"] is not None and final["human_action_required"] is False
    # Verification saw the persisted approval; the officer approval requirement is VALID.
    by_req = {r["requirement"]: r["status"] for r in final["requirements"]}
    assert by_req["COMPLIANCE_OFFICER_APPROVAL"] == "VALID"

    types = [e.event_type for e in events(seeded, run["run_id"])]
    assert "approval_decided" in types
    decided = next(e for e in events(seeded, run["run_id"]) if e.event_type == "approval_decided")
    assert decided.actor == ACTOR and decided.details["decision"] == "APPROVED"
    assert types.index("approval_pending") < types.index("approval_decided")
    assert types[-1] == "status_changed"

    with seeded() as s:
        approval = s.get(Approval, approval_id)
        assert approval is not None and approval.resolved_at is not None


def test_decision_state_survives_app_restart(
    client: TestClient, seeded: sessionmaker[Session]
) -> None:
    run = start(client, "Acme Corp")
    # "Restart": a brand new app instance over the same database file.
    with TestClient(create_app(seeded, FinishLLM, demo_actor=ACTOR)) as restarted:
        assert restarted.get(f"/runs/{run['run_id']}").json()["status"] == "WAITING_APPROVAL"
        assert decide(restarted, run["approval_id"], "APPROVED").status_code == 200
    with TestClient(create_app(seeded, FinishLLM)) as again:
        assert again.get(f"/runs/{run['run_id']}").json()["status"] == "COMPLETED"


def test_rerun_after_approval_completes_from_persisted_approval(client: TestClient) -> None:
    run = start(client, "Acme Corp")
    decide(client, run["approval_id"], "APPROVED")
    second = start(client, "Acme Corp")
    assert second["status"] == "COMPLETED" and second["outcome"] == "COMPLIANT_APPROVED"


def test_rejection_fails_run_and_never_looks_approved(
    client: TestClient, seeded: sessionmaker[Session]
) -> None:
    run = start(client, "Acme Corp")
    body = decide(client, run["approval_id"], "REJECTED", comment="not now").json()
    assert body["approval"]["decision"] == "REJECTED"
    final = client.get(f"/runs/{run['run_id']}").json()
    assert final["status"] == "FAILED" and final["outcome"] == "APPROVAL_REJECTED"
    by_req = {r["requirement"]: r["status"] for r in final["requirements"]}
    assert by_req["COMPLIANCE_OFFICER_APPROVAL"] == "INVALID"

    vendor = next(v for v in client.get("/vendors").json() if v["name"] == "Acme Corp")
    assert vendor["approval_satisfied"] is False and vendor["pending_approval_id"] is None
    # A later run does not turn the rejection into an approval, nor re-request one.
    again = start(client, "Acme Corp")
    assert again["outcome"] == "APPROVAL_REJECTED"
    with seeded() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 2  # Hooli fixture + Acme
    decided = next(e for e in events(seeded, run["run_id"]) if e.event_type == "approval_decided")
    assert decided.details["decision"] == "REJECTED"


# ---- invalid / conflicting decisions ----


def test_duplicate_and_conflicting_decisions_are_rejected(client: TestClient) -> None:
    run = start(client, "Acme Corp")
    assert decide(client, run["approval_id"], "APPROVED").status_code == 200
    for decision in ("APPROVED", "REJECTED"):
        response = decide(client, run["approval_id"], decision)
        assert response.status_code == 409
        assert response.json()["code"] == "approval_not_pending"
    approval = client.get(f"/approvals/{run['approval_id']}").json()
    assert approval["decision"] == "APPROVED"  # the conflicting reject changed nothing


def test_decision_on_already_decided_seed_approval_is_rejected(client: TestClient) -> None:
    approved = client.get("/approvals", params={"status": "APPROVED"}).json()
    assert len(approved) == 1 and approved[0]["vendor_name"] == "Hooli"
    assert approved[0]["is_fixture"] is True and approved[0]["decided_by"] == "seed-fixture"
    response = decide(client, approved[0]["id"], "REJECTED")
    assert response.status_code == 409 and response.json()["code"] == "approval_not_pending"
    assert client.get(f"/approvals/{approved[0]['id']}").json()["decision"] == "APPROVED"


def test_unknown_approval_is_404_and_bad_bodies_are_422(client: TestClient) -> None:
    assert decide(client, "nope", "APPROVED").status_code == 404
    assert client.get("/approvals/nope").status_code == 404
    run = start(client, "Acme Corp")
    url = f"/approvals/{run['approval_id']}/decision"
    for body in (
        {"decision": "PENDING"},
        {"decision": "MAYBE"},
        {},
        {"decision": "APPROVED", "actor": "mallory"},  # the client cannot choose the actor
        {"decision": "APPROVED", "approver": "compliance_officer"},
        {"decision": "APPROVED", "comment": "x" * 1001},
    ):
        response = client.post(url, json=body)
        assert response.status_code == 422, body
        assert response.json()["code"] == "validation_error" and response.json()["errors"]
    assert client.get(f"/approvals/{run['approval_id']}").json()["decision"] == "PENDING"


def _pending_for(factory: sessionmaker[Session], vendor_name: str) -> str:
    """A PENDING request for a vendor whose evidence is deficient (as if evidence went stale)."""
    from app.persistence.models import ComplianceReview

    with factory() as s:
        vendor = s.scalars(select(Vendor).where(Vendor.name == vendor_name)).one()
        review = ComplianceReview(vendor_id=vendor.id)
        s.add(review)
        s.flush()
        approval = Approval(review_id=review.id)
        s.add(approval)
        s.commit()
        return approval.id


def test_cannot_approve_vendor_with_deficient_evidence(
    client: TestClient, seeded: sessionmaker[Session]
) -> None:
    run = start(client, "Globex Industries")
    assert run["outcome"] == "EVIDENCE_DEFICIENT"
    approval_id = _pending_for(seeded, "Globex Industries")
    view = client.get(f"/approvals/{approval_id}").json()
    assert view["can_approve"] is False and view["blocked_reason"]

    response = decide(client, approval_id, "APPROVED")
    assert response.status_code == 409 and response.json()["code"] == "evidence_deficient"
    with seeded() as s:
        approval = s.get(Approval, approval_id)
        assert approval is not None
        assert approval.decision.value == "PENDING" and approval.decided_by is None
    vendor = next(v for v in client.get("/vendors").json() if v["name"] == "Globex Industries")
    assert vendor["evidence_compliant"] is False and vendor["approval_satisfied"] is False
    # Rejecting is still allowed.
    assert decide(client, approval_id, "REJECTED").status_code == 200


def test_evidence_expiring_after_request_blocks_approval_and_is_audited(
    client: TestClient, seeded: sessionmaker[Session]
) -> None:
    run = start(client, "Acme Corp")
    old = (datetime.now(UTC) - timedelta(days=500)).date().isoformat()
    with seeded() as s:
        vendor = s.scalars(select(Vendor).where(Vendor.name == "Acme Corp")).one()
        soc2 = s.scalars(
            select(Document).where(
                Document.vendor_id == vendor.id, Document.document_type == "soc2_report"
            )
        ).one()
        soc2.metadata_ = {"report_type": "type_ii", "period_end": old}
        s.commit()
    response = decide(client, run["approval_id"], "APPROVED")
    assert response.status_code == 409 and response.json()["code"] == "evidence_deficient"
    refused = [
        e for e in events(seeded, run["run_id"]) if e.event_type == "approval_decision_refused"
    ]
    assert len(refused) == 1 and refused[0].actor == ACTOR
    assert client.get(f"/runs/{run['run_id']}").json()["status"] == "WAITING_APPROVAL"


# ---- idempotency and concurrency ----


def test_second_run_shares_the_single_pending_approval_and_both_resume(
    client: TestClient, seeded: sessionmaker[Session]
) -> None:
    first = start(client, "Acme Corp")
    second = start(client, "Acme Corp")
    assert first["approval_id"] == second["approval_id"] and first["run_id"] != second["run_id"]
    with seeded() as s:
        pending = s.scalar(
            select(func.count()).select_from(Approval).where(Approval.decision == "PENDING")
        )
        assert pending == 1
    body = decide(client, first["approval_id"], "APPROVED").json()
    assert {r["run_id"] for r in body["resumed_runs"]} == {first["run_id"], second["run_id"]}
    for run in (first, second):
        assert client.get(f"/runs/{run['run_id']}").json()["status"] == "COMPLETED"


def test_concurrent_decisions_yield_exactly_one_winner(
    client: TestClient, seeded: sessionmaker[Session]
) -> None:
    run = start(client, "Acme Corp")
    codes: list[int] = []
    barrier = threading.Barrier(2)

    def attempt(decision: str) -> None:
        barrier.wait()
        codes.append(decide(client, run["approval_id"], decision).status_code)

    threads = [threading.Thread(target=attempt, args=(d,)) for d in ("APPROVED", "REJECTED")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200, 409]
    # Whatever won, state is internally consistent: one decided approval, one final run.
    approval = client.get(f"/approvals/{run['approval_id']}").json()
    final = client.get(f"/runs/{run['run_id']}").json()
    expected = {"APPROVED": "COMPLETED", "REJECTED": "FAILED"}[approval["decision"]]
    assert final["status"] == expected
    with seeded() as s:
        decided = [
            e
            for e in s.scalars(select(AuditEvent).where(AuditEvent.run_id == run["run_id"]))
            if e.event_type == "approval_decided"
        ]
        assert len(decided) == 1


def test_duplicate_in_flight_run_is_rejected(
    client: TestClient, seeded: sessionmaker[Session]
) -> None:
    with seeded() as s:
        s.add(
            AgentRun(
                goal="Review Acme Corp's compliance status and prepare it for approval.",
                status="EXECUTING",
            )
        )
        s.commit()
    response = client.post("/runs", json={"vendor_name": "Acme Corp"})
    assert response.status_code == 409 and response.json()["code"] == "run_in_progress"
