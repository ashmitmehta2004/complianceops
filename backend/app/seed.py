"""Development seed data: `python -m app.seed`.

Repeatable: records are looked up by natural key (vendor name, policy name+version, vendor+
filename) and never duplicated. Document facts are refreshed on every run because their dates
are relative to today (otherwise the "valid" SOC 2 report would silently expire).
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.compliance import DOC_DPA, DOC_SECURITY_QUESTIONNAIRE, DOC_SOC2_REPORT
from app.domain.enums import ApprovalDecision, WorkflowStatus
from app.persistence.database import (
    create_db_engine,
    create_session_factory,
    init_db,
    session_scope,
)
from app.persistence.models import Approval, ComplianceReview, Document, Policy, Vendor

POLICY_NAME = "vendor-compliance"
POLICY_VERSION = "1.0"
POLICY_RULES: dict[str, Any] = {
    "soc2_type_ii": "required; audit period ended less than 12 months ago",
    "dpa": "required; must be signed",
    "security_questionnaire": "required; every question answered",
    "approval": "a Compliance Officer must approve before finalization",
}


@dataclass(frozen=True)
class VendorSeed:
    name: str
    description: str
    soc2_age_days: int | None = None  # days since audit period end; None = no report
    dpa_signed: bool | None = None  # None = no DPA on file
    questions: tuple[int, int] | None = None  # (answered, total); None = no questionnaire
    officer_approved: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


VENDORS = (
    VendorSeed(
        "Acme Corp",
        "All evidence valid, no approval yet -> run ends WAITING_APPROVAL",
        soc2_age_days=90,
        dpa_signed=True,
        questions=(42, 42),
    ),
    VendorSeed(
        "Globex Industries",
        "Expired SOC 2, unsigned DPA, partial questionnaire -> EVIDENCE_DEFICIENT",
        soc2_age_days=500,
        dpa_signed=False,
        questions=(30, 42),
    ),
    VendorSeed(
        "Initech",
        "No evidence on file at all -> EVIDENCE_DEFICIENT (all MISSING)",
    ),
    VendorSeed(
        "Hooli",
        "Valid evidence and a recorded Compliance Officer approval -> COMPLETED",
        soc2_age_days=120,
        dpa_signed=True,
        questions=(42, 42),
        officer_approved=True,
    ),
)


def _documents(v: VendorSeed, today: date) -> list[tuple[str, str, dict[str, Any]]]:
    """(document_type, filename, facts) for the evidence this vendor has."""
    docs: list[tuple[str, str, dict[str, Any]]] = []
    if v.soc2_age_days is not None:
        period_end = (today - timedelta(days=v.soc2_age_days)).isoformat()
        docs.append(
            (
                DOC_SOC2_REPORT,
                "soc2-type2.pdf",
                {"report_type": "type_ii", "period_end": period_end},
            )
        )
    if v.dpa_signed is not None:
        docs.append((DOC_DPA, "dpa.pdf", {"signed": v.dpa_signed}))
    if v.questions is not None:
        answered, total = v.questions
        docs.append(
            (
                DOC_SECURITY_QUESTIONNAIRE,
                "security-questionnaire.xlsx",
                {"questions_total": total, "questions_answered": answered},
            )
        )
    return docs


def seed(factory: sessionmaker[Session], today: date | None = None) -> dict[str, int]:
    """Insert missing seed records and refresh document facts. Returns counts created."""
    today = today or datetime.now(UTC).date()
    created = {"vendors": 0, "policies": 0, "documents": 0, "reviews": 0, "approvals": 0}
    with session_scope(factory) as s:
        if (
            s.scalars(
                select(Policy).where(Policy.name == POLICY_NAME, Policy.version == POLICY_VERSION)
            ).first()
            is None
        ):
            s.add(Policy(name=POLICY_NAME, version=POLICY_VERSION, rules=POLICY_RULES))
            created["policies"] += 1

        for v in VENDORS:
            vendor = s.scalars(select(Vendor).where(Vendor.name == v.name)).one_or_none()
            if vendor is None:
                vendor = Vendor(name=v.name, metadata_={"seed_scenario": v.description})
                s.add(vendor)
                s.flush()
                created["vendors"] += 1

            for doc_type, filename, facts in _documents(v, today):
                doc = s.scalars(
                    select(Document).where(
                        Document.vendor_id == vendor.id, Document.filename == filename
                    )
                ).one_or_none()
                if doc is None:
                    s.add(
                        Document(
                            vendor_id=vendor.id,
                            document_type=doc_type,
                            filename=filename,
                            source="seed",
                            content=f"[seed] {filename} for {v.name}",
                            extraction_method="seed",
                            metadata_=facts,
                        )
                    )
                    created["documents"] += 1
                else:
                    doc.metadata_ = facts  # reassign so the JSON change is tracked

            if v.officer_approved:
                review = s.scalars(
                    select(ComplianceReview).where(ComplianceReview.vendor_id == vendor.id)
                ).first()
                if review is None:
                    review = ComplianceReview(vendor_id=vendor.id)
                    s.add(review)
                    s.flush()
                    created["reviews"] += 1
                if not review.approvals:
                    s.add(
                        Approval(
                            review_id=review.id,
                            decision=ApprovalDecision.APPROVED,
                            approver="compliance_officer",
                            decided_by="seed-fixture",
                            comment="Demo fixture data, not a real Compliance Officer decision.",
                            resolved_at=datetime.now(UTC),
                        )
                    )
                    created["approvals"] += 1
                # Evidence is valid and the fixture approval exists, so the review is complete.
                # Only a never-advanced review is touched, so real run results are not overwritten.
                if review.status is WorkflowStatus.RECEIVED:
                    review.status = WorkflowStatus.COMPLETED
    return created


def main() -> None:
    engine = create_db_engine()
    init_db(engine)
    created = seed(create_session_factory(engine))
    print(f"Seeded {engine.url.render_as_string(hide_password=True)}: {created}")
    for v in VENDORS:
        print(f"  {v.name}: {v.description}")


if __name__ == "__main__":
    main()
