"""POST /vendors: validation, duplicates, persistence and policy integration."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import LLMResponse
from app.main import create_app
from app.persistence.models import ComplianceReview, Document, Vendor
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


def create(client: TestClient, **body: Any) -> Any:
    return client.post("/vendors", json=body)


def test_create_persists_and_trims(client: TestClient, session_factory: Any) -> None:
    response = create(client, name="  Umbrella Ltd  ", description="Payroll provider")
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Umbrella Ltd"
    assert body["description"] == "Payroll provider"
    with session_factory() as s:
        vendor = s.scalars(select(Vendor).where(Vendor.name == "Umbrella Ltd")).one()
        assert vendor.id == body["id"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"name": "Ok", "description": "y" * 501},
        {"name": "Ok", "unexpected": 1},
        {"name": 5},
    ],
)
def test_invalid_input_is_rejected_consistently(client: TestClient, payload: Any) -> None:
    response = create(client, **payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"]


def test_duplicate_name_is_rejected_case_insensitively(
    client: TestClient, session_factory: Any
) -> None:
    for name in ("Acme Corp", "acme corp", " ACME CORP "):
        response = create(client, name=name)
        assert response.status_code == 409
        assert response.json()["code"] == "vendor_exists"
    assert create(client, name="Newco").status_code == 201
    assert create(client, name="newco").status_code == 409
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Vendor)) == 5


def test_new_vendor_appears_in_list_and_detail(client: TestClient) -> None:
    vendor_id = create(client, name="Umbrella Ltd").json()["id"]
    names = {v["name"] for v in client.get("/vendors").json()}
    assert names == {"Acme Corp", "Globex Industries", "Initech", "Hooli", "Umbrella Ltd"}
    detail = client.get(f"/vendors/{vendor_id}").json()
    assert detail["name"] == "Umbrella Ltd"
    assert detail["documents"] == detail["reviews"] == detail["runs"] == []


def test_new_vendor_without_evidence_is_deficient_and_has_no_review(
    client: TestClient, session_factory: Any
) -> None:
    body = create(client, name="Umbrella Ltd").json()
    assert body["evidence_compliant"] is False
    assert body["approval_satisfied"] is False
    assert body["review_id"] is None
    assert {r["status"] for r in body["requirements"]} == {"MISSING"}
    with session_factory() as s:
        assert s.scalars(select(Document).where(Document.vendor_id == body["id"])).first() is None
        assert (
            s.scalars(
                select(ComplianceReview).where(ComplianceReview.vendor_id == body["id"])
            ).first()
            is None
        )


def test_new_vendor_review_run_ends_evidence_deficient(
    client: TestClient, session_factory: Any
) -> None:
    vendor_id = create(client, name="Umbrella Ltd").json()["id"]
    run = client.post("/runs", json={"vendor_name": "Umbrella Ltd"})
    assert run.status_code == 201, run.text
    assert run.json()["outcome"] == "EVIDENCE_DEFICIENT"
    again = client.post("/runs", json={"vendor_name": "Umbrella Ltd"})
    assert again.status_code == 201, again.text
    with session_factory() as s:
        reviews = s.scalars(
            select(ComplianceReview).where(ComplianceReview.vendor_id == vendor_id)
        ).all()
        assert len(reviews) == 1  # re-running reuses the review


def test_seeded_vendors_are_unchanged_by_creation(client: TestClient) -> None:
    before = {v["name"]: v for v in client.get("/vendors").json()}
    create(client, name="Umbrella Ltd")
    after = {v["name"]: v for v in client.get("/vendors").json()}
    for name, vendor in before.items():
        assert after[name] == vendor
    assert after["Hooli"]["evidence_compliant"] and after["Hooli"]["approval_satisfied"]
    assert after["Acme Corp"]["evidence_compliant"] and not after["Acme Corp"]["approval_satisfied"]
    assert not after["Initech"]["evidence_compliant"]
