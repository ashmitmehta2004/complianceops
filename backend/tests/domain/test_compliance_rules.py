import copy
import json
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from app.domain.compliance import (
    DOC_DPA,
    DOC_SECURITY_QUESTIONNAIRE,
    DOC_SOC2_REPORT,
    ApprovalRecord,
    ComplianceEvaluation,
    CompliancePolicy,
    ReasonCode,
    RequirementKey,
    RequirementResult,
    RequirementStatus,
    SubmittedDocument,
)
from app.domain.compliance_rules import add_months, evaluate_compliance
from app.domain.enums import ApprovalDecision

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 1, 1, tzinfo=UTC)

Status = RequirementStatus
Reason = ReasonCode
Key = RequirementKey


def doc(
    doc_type: str, facts: dict[str, Any], doc_id: str = "d1", submitted_at: datetime = T0
) -> SubmittedDocument:
    return SubmittedDocument(
        id=doc_id, document_type=doc_type, submitted_at=submitted_at, facts=facts
    )


def soc2(period_end: object = "2026-03-31", **extra: Any) -> SubmittedDocument:
    return doc(DOC_SOC2_REPORT, {"report_type": "type_ii", "period_end": period_end, **extra})


def dpa(signed: object = True) -> SubmittedDocument:
    return doc(DOC_DPA, {"signed": signed}, "dpa1")


def questionnaire(total: object = 10, answered: object = 10) -> SubmittedDocument:
    return doc(
        DOC_SECURITY_QUESTIONNAIRE,
        {"questions_total": total, "questions_answered": answered},
        "q1",
    )


def approval(
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
    role: str | None = "compliance_officer",
    approval_id: str = "a1",
    requested_at: datetime = T0,
) -> ApprovalRecord:
    return ApprovalRecord(
        id=approval_id, decision=decision, approver_role=role, requested_at=requested_at
    )


def good_documents() -> list[SubmittedDocument]:
    return [soc2(), dpa(), questionnaire()]


def result_for(evaluation: ComplianceEvaluation, key: RequirementKey) -> RequirementResult:
    return next(r for r in evaluation.results if r.requirement is key)


def evaluate_one(
    key: RequirementKey,
    documents: list[SubmittedDocument],
    when: datetime = NOW,
) -> RequirementResult:
    return result_for(evaluate_compliance(documents, [], when), key)


# valid
def test_everything_valid_allows_finalization() -> None:
    evaluation = evaluate_compliance(good_documents(), [approval()], NOW)

    assert [r.status for r in evaluation.results] == [Status.VALID] * 4
    assert [r.requirement for r in evaluation.results] == list(Key)
    assert evaluation.evidence_compliant
    assert evaluation.approval_satisfied
    assert evaluation.can_finalize


def test_valid_soc2_reports_period_and_expiry() -> None:
    result = evaluate_one(Key.SOC2_TYPE_II, [soc2("2026-03-31")])

    assert (result.status, result.reason) == (Status.VALID, Reason.OK)
    assert result.source_id == "d1"
    assert result.period_end == date(2026, 3, 31)
    assert result.expires_on == date(2027, 3, 31)


def test_evidence_can_be_compliant_while_approval_is_not() -> None:
    evaluation = evaluate_compliance(good_documents(), [], NOW)

    assert evaluation.evidence_compliant
    assert not evaluation.approval_satisfied
    assert not evaluation.can_finalize


# missing
def test_nothing_submitted_is_all_missing() -> None:
    evaluation = evaluate_compliance([], [], NOW)

    assert [r.status for r in evaluation.results] == [Status.MISSING] * 4
    assert [r.reason for r in evaluation.results] == [
        Reason.NO_DOCUMENT,
        Reason.NO_DOCUMENT,
        Reason.NO_DOCUMENT,
        Reason.NO_APPROVAL,
    ]
    assert not evaluation.can_finalize


def test_other_document_types_do_not_satisfy_a_requirement() -> None:
    result = evaluate_one(Key.DPA, [doc("invoice", {"signed": True})])

    assert result.status is Status.MISSING


@pytest.mark.parametrize("missing", [DOC_SOC2_REPORT, DOC_DPA, DOC_SECURITY_QUESTIONNAIRE])
def test_one_missing_document_blocks_finalization(missing: str) -> None:
    documents = [d for d in good_documents() if d.document_type != missing]

    evaluation = evaluate_compliance(documents, [approval()], NOW)

    assert not evaluation.can_finalize
    assert [r.status for r in evaluation.results].count(Status.MISSING) == 1


# expired
@pytest.mark.parametrize(
    ("period_end", "status"),
    [
        ("2025-10-10", Status.VALID),  # expires tomorrow
        ("2025-10-09", Status.EXPIRED),  # exactly 12 months old: not "less than"
        ("2025-10-08", Status.EXPIRED),
        ("2020-01-01", Status.EXPIRED),
    ],
)
def test_soc2_age_boundary(period_end: str, status: RequirementStatus) -> None:
    result = evaluate_one(Key.SOC2_TYPE_II, [soc2(period_end)])

    assert result.status is status
    if status is Status.EXPIRED:
        assert result.reason is Reason.REPORT_OLDER_THAN_MAX_AGE


def test_expired_soc2_still_reports_dates() -> None:
    result = evaluate_one(Key.SOC2_TYPE_II, [soc2("2024-12-31")])

    assert result.period_end == date(2024, 12, 31)
    assert result.expires_on == date(2025, 12, 31)


def test_leap_day_report_expires_on_28_february() -> None:
    documents = [soc2("2024-02-29")]

    day_before = evaluate_one(Key.SOC2_TYPE_II, documents, datetime(2025, 2, 27, tzinfo=UTC))
    on_expiry = evaluate_one(Key.SOC2_TYPE_II, documents, datetime(2025, 2, 28, tzinfo=UTC))

    assert day_before.status is Status.VALID
    assert on_expiry.status is Status.EXPIRED


def test_evaluation_date_is_taken_in_utc() -> None:
    # 2026-10-09 23:30 at UTC-5 is already 2026-10-10 in UTC, when this report has expired.
    late_evening = datetime(2026, 10, 9, 23, 30, tzinfo=timezone(timedelta(hours=-5)))

    result = evaluate_one(Key.SOC2_TYPE_II, [soc2("2025-10-10")], late_evening)

    assert result.status is Status.EXPIRED


def test_max_age_comes_from_policy() -> None:
    policy = CompliancePolicy(soc2_max_age_months=6)

    evaluation = evaluate_compliance([soc2("2026-03-31")], [], NOW, policy)

    assert result_for(evaluation, Key.SOC2_TYPE_II).status is Status.EXPIRED


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2024, 2, 29), 12, date(2025, 2, 28)),
        (date(2025, 1, 31), 1, date(2025, 2, 28)),
        (date(2025, 11, 30), 3, date(2026, 2, 28)),
        (date(2025, 12, 15), 12, date(2026, 12, 15)),
        (date(2025, 12, 31), 2, date(2026, 2, 28)),
    ],
)
def test_add_months_clamps_to_month_end(start: date, months: int, expected: date) -> None:
    assert add_months(start, months) == expected


# incomplete
def test_soc2_without_report_date_is_incomplete() -> None:
    result = evaluate_one(Key.SOC2_TYPE_II, [doc(DOC_SOC2_REPORT, {"report_type": "type_ii"})])

    assert (result.status, result.reason) == (Status.INCOMPLETE, Reason.REPORT_DATE_MISSING)


def test_soc2_without_report_type_is_incomplete() -> None:
    result = evaluate_one(Key.SOC2_TYPE_II, [doc(DOC_SOC2_REPORT, {"period_end": "2026-03-31"})])

    assert (result.status, result.reason) == (Status.INCOMPLETE, Reason.MISSING_FACTS)


@pytest.mark.parametrize("signed", [False, None])
def test_unsigned_dpa_is_incomplete(signed: object) -> None:
    result = evaluate_one(Key.DPA, [dpa(signed)])

    assert (result.status, result.reason) == (Status.INCOMPLETE, Reason.NOT_SIGNED)


@pytest.mark.parametrize(("total", "answered"), [(10, 9), (10, 0), (0, 0)])
def test_partly_answered_questionnaire_is_incomplete(total: int, answered: int) -> None:
    result = evaluate_one(Key.SECURITY_QUESTIONNAIRE, [questionnaire(total, answered)])

    assert (result.status, result.reason) == (Status.INCOMPLETE, Reason.QUESTIONS_UNANSWERED)


def test_questionnaire_without_counts_is_incomplete() -> None:
    result = evaluate_one(Key.SECURITY_QUESTIONNAIRE, [doc(DOC_SECURITY_QUESTIONNAIRE, {})])

    assert (result.status, result.reason) == (Status.INCOMPLETE, Reason.MISSING_FACTS)


def test_pending_approval_is_incomplete() -> None:
    evaluation = evaluate_compliance([], [approval(ApprovalDecision.PENDING, None)], NOW)

    result = result_for(evaluation, Key.COMPLIANCE_OFFICER_APPROVAL)
    assert (result.status, result.reason) == (Status.INCOMPLETE, Reason.APPROVAL_PENDING)


# invalid
def test_type_i_report_is_invalid() -> None:
    bad = doc(DOC_SOC2_REPORT, {"report_type": "type_i", "period_end": "2026-03-31"})

    result = evaluate_one(Key.SOC2_TYPE_II, [bad])

    assert (result.status, result.reason) == (Status.INVALID, Reason.REPORT_TYPE_NOT_TYPE_II)


def test_future_report_date_is_invalid() -> None:
    result = evaluate_one(Key.SOC2_TYPE_II, [soc2("2026-10-10")])

    assert (result.status, result.reason) == (Status.INVALID, Reason.REPORT_DATE_IN_FUTURE)
    assert result.period_end == date(2026, 10, 10)


def test_report_dated_today_is_valid() -> None:
    assert evaluate_one(Key.SOC2_TYPE_II, [soc2("2026-10-09")]).status is Status.VALID


@pytest.mark.parametrize(
    "period_end", ["31/03/2026", "2026-3-1", "20260331", "2026-02-30", 20260331]
)
def test_malformed_report_date_is_invalid(period_end: object) -> None:
    result = evaluate_one(Key.SOC2_TYPE_II, [soc2(period_end)])

    assert (result.status, result.reason) == (Status.INVALID, Reason.MALFORMED_FACTS)


def test_non_string_report_type_is_invalid() -> None:
    bad = doc(DOC_SOC2_REPORT, {"report_type": 2, "period_end": "2026-03-31"})

    assert evaluate_one(Key.SOC2_TYPE_II, [bad]).reason is Reason.MALFORMED_FACTS


@pytest.mark.parametrize("signed", ["yes", 1, "true"])
def test_non_boolean_signed_is_invalid(signed: object) -> None:
    result = evaluate_one(Key.DPA, [dpa(signed)])

    assert (result.status, result.reason) == (Status.INVALID, Reason.MALFORMED_FACTS)


@pytest.mark.parametrize(
    ("total", "answered"), [(5, 6), (-1, 0), ("10", 10), (10, True), (10.0, 10)]
)
def test_malformed_questionnaire_counts_are_invalid(total: object, answered: object) -> None:
    result = evaluate_one(Key.SECURITY_QUESTIONNAIRE, [questionnaire(total, answered)])

    assert (result.status, result.reason) == (Status.INVALID, Reason.MALFORMED_FACTS)


def test_rejected_approval_is_invalid() -> None:
    evaluation = evaluate_compliance([], [approval(ApprovalDecision.REJECTED)], NOW)

    result = result_for(evaluation, Key.COMPLIANCE_OFFICER_APPROVAL)
    assert (result.status, result.reason) == (Status.INVALID, Reason.APPROVAL_REJECTED)


@pytest.mark.parametrize("role", [None, "engineer", "Compliance Officer"])
def test_approval_by_non_officer_is_invalid(role: str | None) -> None:
    evaluation = evaluate_compliance(good_documents(), [approval(role=role)], NOW)

    result = result_for(evaluation, Key.COMPLIANCE_OFFICER_APPROVAL)
    assert (result.status, result.reason) == (
        Status.INVALID,
        Reason.APPROVER_NOT_COMPLIANCE_OFFICER,
    )
    assert not evaluation.can_finalize


def test_any_invalid_requirement_blocks_finalization() -> None:
    documents = [soc2("2020-01-01"), dpa(), questionnaire()]

    evaluation = evaluate_compliance(documents, [approval()], NOW)

    assert evaluation.approval_satisfied
    assert not evaluation.evidence_compliant
    assert not evaluation.can_finalize


# several documents / approvals for one requirement
def test_any_valid_document_satisfies_the_requirement() -> None:
    old_valid = doc(
        DOC_SOC2_REPORT,
        {"report_type": "type_ii", "period_end": "2026-01-31"},
        "old",
        datetime(2026, 2, 1, tzinfo=UTC),
    )
    newer_bad = doc(
        DOC_SOC2_REPORT,
        {"report_type": "type_i", "period_end": "2026-06-30"},
        "new",
        datetime(2026, 7, 1, tzinfo=UTC),
    )

    result = evaluate_one(Key.SOC2_TYPE_II, [newer_bad, old_valid])

    assert (result.status, result.source_id) == (Status.VALID, "old")


def test_without_a_valid_document_the_newest_decides() -> None:
    expired_old = doc(
        DOC_SOC2_REPORT,
        {"report_type": "type_ii", "period_end": "2020-01-01"},
        "old",
        datetime(2024, 1, 1, tzinfo=UTC),
    )
    incomplete_new = doc(
        DOC_SOC2_REPORT, {"report_type": "type_ii"}, "new", datetime(2026, 1, 1, tzinfo=UTC)
    )

    for documents in ([expired_old, incomplete_new], [incomplete_new, expired_old]):
        result = evaluate_one(Key.SOC2_TYPE_II, documents)
        assert (result.status, result.source_id) == (Status.INCOMPLETE, "new")


def test_latest_approval_record_decides() -> None:
    approved = approval(ApprovalDecision.APPROVED, approval_id="a1", requested_at=T0)
    rejected_later = approval(
        ApprovalDecision.REJECTED, approval_id="a2", requested_at=T0 + timedelta(days=1)
    )

    for approvals in ([approved, rejected_later], [rejected_later, approved]):
        result = result_for(
            evaluate_compliance([], approvals, NOW), Key.COMPLIANCE_OFFICER_APPROVAL
        )
        assert (result.status, result.source_id) == (Status.INVALID, "a2")


def test_new_approval_after_rejection_can_be_valid() -> None:
    rejected = approval(ApprovalDecision.REJECTED, approval_id="a1", requested_at=T0)
    approved_later = approval(approval_id="a2", requested_at=T0 + timedelta(days=1))

    evaluation = evaluate_compliance(good_documents(), [rejected, approved_later], NOW)

    assert evaluation.can_finalize


# determinism and purity
def test_evaluation_is_deterministic_and_does_not_mutate_inputs() -> None:
    documents = [*good_documents(), soc2("2020-01-01")]
    approvals = [approval()]
    snapshot = copy.deepcopy((documents, approvals))

    first = evaluate_compliance(documents, approvals, NOW)
    second = evaluate_compliance(list(reversed(documents)), approvals, NOW)

    assert first == second
    assert (documents, approvals) == snapshot


def test_evaluation_serializes_to_plain_json() -> None:
    evaluation = evaluate_compliance(good_documents(), [approval()], NOW)

    payload = json.loads(evaluation.model_dump_json())

    assert payload["can_finalize"] is True
    assert payload["results"][0]["requirement"] == "SOC2_TYPE_II"
    assert payload["results"][0]["status"] == "VALID"
    assert payload["results"][0]["expires_on"] == "2027-03-31"


def test_naive_evaluation_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_compliance([], [], datetime(2026, 10, 9))  # noqa: DTZ001
