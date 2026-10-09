"""Deterministic compliance evaluation. Pure: no database, clock, network or LLM.

Policy:
- SOC 2 Type II is required and its audit period must have ended less than 12 months ago.
- A DPA is required and must be signed.
- A security questionnaire is required and must be fully answered.
- A Compliance Officer must have approved before final approval.
"""

import calendar
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

from app.domain.compliance import (
    DEFAULT_POLICY,
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
from app.domain.enums import ApprovalDecision

_Status = RequirementStatus
_Reason = ReasonCode


def add_months(start: date, months: int) -> date:
    """Calendar-month arithmetic, clamping to the end of a shorter month (29 Feb + 12m = 28 Feb)."""
    year, month_index = divmod(start.year * 12 + start.month - 1 + months, 12)
    month = month_index + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _result(
    key: RequirementKey,
    status: RequirementStatus,
    reason: ReasonCode,
    source_id: str | None = None,
    *,
    period_end: date | None = None,
    expires_on: date | None = None,
) -> RequirementResult:
    return RequirementResult(
        requirement=key,
        status=status,
        reason=reason,
        source_id=source_id,
        period_end=period_end,
        expires_on=expires_on,
    )


def _parse_iso_date(value: object) -> date | None:
    """Strict YYYY-MM-DD only; anything else is malformed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _evaluate_soc2(
    doc: SubmittedDocument, today: date, policy: CompliancePolicy
) -> RequirementResult:
    key = RequirementKey.SOC2_TYPE_II
    facts = doc.facts

    report_type = facts.get("report_type")
    if report_type is None:
        return _result(key, _Status.INCOMPLETE, _Reason.MISSING_FACTS, doc.id)
    if not isinstance(report_type, str):
        return _result(key, _Status.INVALID, _Reason.MALFORMED_FACTS, doc.id)
    if report_type != "type_ii":
        return _result(key, _Status.INVALID, _Reason.REPORT_TYPE_NOT_TYPE_II, doc.id)

    raw_period_end = facts.get("period_end")
    if raw_period_end is None:
        return _result(key, _Status.INCOMPLETE, _Reason.REPORT_DATE_MISSING, doc.id)
    period_end = _parse_iso_date(raw_period_end)
    if period_end is None:
        return _result(key, _Status.INVALID, _Reason.MALFORMED_FACTS, doc.id)
    if period_end > today:
        return _result(
            key, _Status.INVALID, _Reason.REPORT_DATE_IN_FUTURE, doc.id, period_end=period_end
        )

    expires_on = add_months(period_end, policy.soc2_max_age_months)
    if today >= expires_on:
        return _result(
            key,
            _Status.EXPIRED,
            _Reason.REPORT_OLDER_THAN_MAX_AGE,
            doc.id,
            period_end=period_end,
            expires_on=expires_on,
        )
    return _result(
        key, _Status.VALID, _Reason.OK, doc.id, period_end=period_end, expires_on=expires_on
    )


def _evaluate_dpa(
    doc: SubmittedDocument, today: date, policy: CompliancePolicy
) -> RequirementResult:
    key = RequirementKey.DPA
    signed = doc.facts.get("signed")
    if signed is None or signed is False:
        return _result(key, _Status.INCOMPLETE, _Reason.NOT_SIGNED, doc.id)
    if signed is not True:
        return _result(key, _Status.INVALID, _Reason.MALFORMED_FACTS, doc.id)
    return _result(key, _Status.VALID, _Reason.OK, doc.id)


def _evaluate_questionnaire(
    doc: SubmittedDocument, today: date, policy: CompliancePolicy
) -> RequirementResult:
    key = RequirementKey.SECURITY_QUESTIONNAIRE
    total = doc.facts.get("questions_total")
    answered = doc.facts.get("questions_answered")
    if total is None or answered is None:
        return _result(key, _Status.INCOMPLETE, _Reason.MISSING_FACTS, doc.id)
    if not _is_count(total) or not _is_count(answered) or answered > total:
        return _result(key, _Status.INVALID, _Reason.MALFORMED_FACTS, doc.id)
    if total == 0 or answered < total:
        return _result(key, _Status.INCOMPLETE, _Reason.QUESTIONS_UNANSWERED, doc.id)
    return _result(key, _Status.VALID, _Reason.OK, doc.id)


_DocumentRule = Callable[[SubmittedDocument, date, CompliancePolicy], RequirementResult]

# (requirement, document type, rule) in report order.
_DOCUMENT_RULES: list[tuple[RequirementKey, str, _DocumentRule]] = [
    (RequirementKey.SOC2_TYPE_II, DOC_SOC2_REPORT, _evaluate_soc2),
    (RequirementKey.DPA, DOC_DPA, _evaluate_dpa),
    (RequirementKey.SECURITY_QUESTIONNAIRE, DOC_SECURITY_QUESTIONNAIRE, _evaluate_questionnaire),
]


def _evaluate_requirement_documents(
    key: RequirementKey,
    docs: list[SubmittedDocument],
    rule: _DocumentRule,
    today: date,
    policy: CompliancePolicy,
) -> RequirementResult:
    """Any valid document satisfies the requirement; otherwise the newest document decides."""
    if not docs:
        return _result(key, _Status.MISSING, _Reason.NO_DOCUMENT)
    newest_first = sorted(docs, key=lambda d: (d.submitted_at, d.id), reverse=True)
    results = [rule(doc, today, policy) for doc in newest_first]
    return next((r for r in results if r.status is _Status.VALID), results[0])


def _evaluate_approval(
    approvals: Sequence[ApprovalRecord], policy: CompliancePolicy
) -> RequirementResult:
    """The latest approval record decides, so a later rejection overrides an earlier approval."""
    key = RequirementKey.COMPLIANCE_OFFICER_APPROVAL
    if not approvals:
        return _result(key, _Status.MISSING, _Reason.NO_APPROVAL)
    latest = max(approvals, key=lambda a: (a.requested_at, a.id))
    match latest.decision:
        case ApprovalDecision.PENDING:
            return _result(key, _Status.INCOMPLETE, _Reason.APPROVAL_PENDING, latest.id)
        case ApprovalDecision.REJECTED:
            return _result(key, _Status.INVALID, _Reason.APPROVAL_REJECTED, latest.id)
        case ApprovalDecision.APPROVED:
            if latest.approver_role != policy.compliance_officer_role:
                return _result(
                    key, _Status.INVALID, _Reason.APPROVER_NOT_COMPLIANCE_OFFICER, latest.id
                )
            return _result(key, _Status.VALID, _Reason.OK, latest.id)


def evaluate_compliance(
    documents: Sequence[SubmittedDocument],
    approvals: Sequence[ApprovalRecord],
    evaluated_at: datetime,
    policy: CompliancePolicy = DEFAULT_POLICY,
) -> ComplianceEvaluation:
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    today = evaluated_at.astimezone(UTC).date()

    results = [
        _evaluate_requirement_documents(
            key, [d for d in documents if d.document_type == doc_type], rule, today, policy
        )
        for key, doc_type, rule in _DOCUMENT_RULES
    ]
    results.append(_evaluate_approval(approvals, policy))
    return ComplianceEvaluation(evaluated_at=evaluated_at, results=results)
