from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import EvidenceStatus
from app.persistence.models import ComplianceReview, Document, EvidenceCheck, Policy, Vendor
from app.tools.base import ToolErrorCode, ToolResult
from app.tools.permissions import Permission, Principal
from app.tools.readonly import (
    DocumentOutput,
    PolicyOutput,
    SearchEvidenceOutput,
    VendorOutput,
    build_default_registry,
)
from app.tools.runtime import ToolRuntime

ALL = Principal("agent", frozenset(Permission))


@dataclass
class Seed:
    vendor_id: str
    review_id: str
    document_id: str
    policy_id: str


@pytest.fixture
def seed(session: Session) -> Seed:
    vendor = Vendor(name="Acme Corp", metadata_={"country": "DE"})
    document = Document(
        vendor=vendor,
        document_type="insurance_certificate",
        filename="cert.pdf",
        source="upload",
        content="0123456789",
        extraction_method="text",
    )
    review = ComplianceReview(vendor=vendor)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    session.add_all(
        [
            document,
            review,
            EvidenceCheck(
                review=review,
                document=document,
                requirement="Valid insurance certificate",
                status=EvidenceStatus.VALID,
                reason="Expires 2027",
            ),
            EvidenceCheck(
                review=review, requirement="Tax certificate", status=EvidenceStatus.MISSING
            ),
            EvidenceCheck(
                review=review,
                requirement="Data protection addendum",
                status=EvidenceStatus.INVALID,
                reason="Unsigned INSURANCE rider",
            ),
            Policy(name="Onboarding", version="1.0", rules={"v": 1}, created_at=base),
            Policy(
                name="Onboarding",
                version="2.0",
                rules={"v": 2},
                created_at=base + timedelta(days=1),
            ),
        ]
    )
    session.commit()
    policy_id = session.query(Policy).filter_by(version="1.0").one().id
    return Seed(vendor.id, review.id, document.id, policy_id)


@pytest.fixture
def runtime(session_factory: sessionmaker[Session]) -> ToolRuntime:
    return ToolRuntime(build_default_registry(), session_factory)


def run(runtime: ToolRuntime, tool: str, args: dict[str, object]) -> ToolResult:
    return runtime.execute(tool, args, ALL)


def error_code(result: ToolResult) -> ToolErrorCode:
    assert result.error is not None
    return result.error.code


# get_vendor
def test_get_vendor_by_id_and_by_name(runtime: ToolRuntime, seed: Seed) -> None:
    by_id = run(runtime, "get_vendor", {"vendor_id": seed.vendor_id})
    by_name = run(runtime, "get_vendor", {"name": "Acme Corp"})

    assert isinstance(by_id.output, VendorOutput)
    assert by_id.output.name == "Acme Corp"
    assert by_id.output.metadata == {"country": "DE"}
    assert by_id.output == by_name.output


def test_get_vendor_not_found(runtime: ToolRuntime, seed: Seed) -> None:
    assert error_code(run(runtime, "get_vendor", {"name": "Nobody"})) is ToolErrorCode.NOT_FOUND


@pytest.mark.parametrize("args", [{}, {"vendor_id": "a", "name": "b"}])
def test_get_vendor_requires_exactly_one_identifier(
    runtime: ToolRuntime, args: dict[str, object]
) -> None:
    assert error_code(run(runtime, "get_vendor", args)) is ToolErrorCode.INVALID_ARGUMENTS


# get_policy
def test_get_policy_by_id(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "get_policy", {"policy_id": seed.policy_id})

    assert isinstance(result.output, PolicyOutput)
    assert result.output.version == "1.0"
    assert result.output.rules == {"v": 1}


def test_get_policy_by_name_returns_newest_version(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "get_policy", {"name": "Onboarding"})

    assert isinstance(result.output, PolicyOutput)
    assert result.output.version == "2.0"


def test_get_policy_by_name_and_version(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "get_policy", {"name": "Onboarding", "version": "1.0"})

    assert isinstance(result.output, PolicyOutput)
    assert result.output.rules == {"v": 1}


def test_get_policy_not_found(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "get_policy", {"name": "Onboarding", "version": "9.9"})

    assert error_code(result) is ToolErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    "args", [{}, {"policy_id": "a", "name": "b"}, {"policy_id": "a", "version": "1.0"}]
)
def test_get_policy_rejects_invalid_selectors(
    runtime: ToolRuntime, args: dict[str, object]
) -> None:
    assert error_code(run(runtime, "get_policy", args)) is ToolErrorCode.INVALID_ARGUMENTS


# search_evidence
def test_search_evidence_lists_checks_of_one_review_in_stable_order(
    runtime: ToolRuntime, seed: Seed
) -> None:
    result = run(runtime, "search_evidence", {"review_id": seed.review_id})

    assert isinstance(result.output, SearchEvidenceOutput)
    assert [r.requirement for r in result.output.results] == [
        "Data protection addendum",
        "Tax certificate",
        "Valid insurance certificate",
    ]
    assert not result.output.truncated


def test_search_evidence_filters_by_status(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "search_evidence", {"review_id": seed.review_id, "status": "MISSING"})

    assert isinstance(result.output, SearchEvidenceOutput)
    assert [r.requirement for r in result.output.results] == ["Tax certificate"]


def test_search_evidence_text_matches_requirement_or_reason_case_insensitively(
    runtime: ToolRuntime, seed: Seed
) -> None:
    result = run(runtime, "search_evidence", {"review_id": seed.review_id, "text": "insurance"})

    assert isinstance(result.output, SearchEvidenceOutput)
    assert [r.requirement for r in result.output.results] == [
        "Data protection addendum",
        "Valid insurance certificate",
    ]


def test_search_evidence_text_treats_wildcards_literally(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "search_evidence", {"review_id": seed.review_id, "text": "%"})

    assert isinstance(result.output, SearchEvidenceOutput)
    assert result.output.results == []


def test_search_evidence_limit_reports_truncation(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "search_evidence", {"review_id": seed.review_id, "limit": 2})

    assert isinstance(result.output, SearchEvidenceOutput)
    assert len(result.output.results) == 2
    assert result.output.truncated


def test_search_evidence_unknown_review(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "search_evidence", {"review_id": "nope"})

    assert error_code(result) is ToolErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"review_id": "r", "status": "BOGUS"},
        {"review_id": "r", "limit": 0},
        {"review_id": "r", "limit": 201},
    ],
)
def test_search_evidence_rejects_invalid_arguments(
    runtime: ToolRuntime, args: dict[str, object]
) -> None:
    assert error_code(run(runtime, "search_evidence", args)) is ToolErrorCode.INVALID_ARGUMENTS


# read_document
def test_read_document_returns_content(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "read_document", {"document_id": seed.document_id})

    assert isinstance(result.output, DocumentOutput)
    assert result.output.content == "0123456789"
    assert result.output.total_chars == 10
    assert not result.output.truncated
    assert result.output.vendor_id == seed.vendor_id


def test_read_document_truncates_to_max_chars(runtime: ToolRuntime, seed: Seed) -> None:
    result = run(runtime, "read_document", {"document_id": seed.document_id, "max_chars": 4})

    assert isinstance(result.output, DocumentOutput)
    assert result.output.content == "0123"
    assert result.output.total_chars == 10
    assert result.output.truncated


def test_read_document_not_found(runtime: ToolRuntime, seed: Seed) -> None:
    assert error_code(run(runtime, "read_document", {"document_id": "nope"})) is (
        ToolErrorCode.NOT_FOUND
    )


# permissions across the default tools
@pytest.mark.parametrize(
    ("tool", "args", "permission"),
    [
        ("get_vendor", {"name": "Acme Corp"}, Permission.VENDOR_READ),
        ("get_policy", {"name": "Onboarding"}, Permission.POLICY_READ),
        ("search_evidence", {"review_id": "r"}, Permission.EVIDENCE_READ),
        ("read_document", {"document_id": "d"}, Permission.DOCUMENT_READ),
    ],
)
def test_each_tool_requires_its_own_permission(
    runtime: ToolRuntime,
    seed: Seed,
    tool: str,
    args: dict[str, object],
    permission: Permission,
) -> None:
    without = Principal("p", frozenset(Permission) - {permission})

    assert error_code(runtime.execute(tool, args, without)) is ToolErrorCode.PERMISSION_DENIED
    allowed = runtime.execute(tool, args, Principal("p", frozenset({permission})))
    assert allowed.error is None or allowed.error.code is not ToolErrorCode.PERMISSION_DENIED
