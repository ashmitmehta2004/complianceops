"""Human-readable explanations of deterministic requirement results.

Pure lookup from the reason code: the text restates what the policy engine decided and never
adds a judgement of its own.
"""

from app.domain.compliance import ReasonCode, RequirementKey, RequirementResult

LABELS = {
    RequirementKey.SOC2_TYPE_II: "SOC 2 Type II report",
    RequirementKey.DPA: "Data Processing Agreement (DPA)",
    RequirementKey.SECURITY_QUESTIONNAIRE: "Security questionnaire",
    RequirementKey.COMPLIANCE_OFFICER_APPROVAL: "Compliance Officer approval",
}

_REASONS: dict[ReasonCode, str] = {
    ReasonCode.OK: "Requirement satisfied.",
    ReasonCode.NO_DOCUMENT: "No document has been submitted for this requirement.",
    ReasonCode.MISSING_FACTS: "The document is on file but required information is missing.",
    ReasonCode.MALFORMED_FACTS: "The document's recorded facts are malformed and not trusted.",
    ReasonCode.REPORT_TYPE_NOT_TYPE_II: "The report is not a SOC 2 Type II report.",
    ReasonCode.REPORT_DATE_MISSING: "The report has no audit period end date.",
    ReasonCode.REPORT_DATE_IN_FUTURE: "The report's audit period end date is in the future.",
    ReasonCode.REPORT_OLDER_THAN_MAX_AGE: (
        "The audit period ended more than 12 months ago; a newer report is required."
    ),
    ReasonCode.NOT_SIGNED: "The DPA is on file but has not been signed.",
    ReasonCode.QUESTIONS_UNANSWERED: "The questionnaire is not fully answered.",
    ReasonCode.NO_APPROVAL: "No approval has been requested or recorded.",
    ReasonCode.APPROVAL_PENDING: "An approval request is pending a human decision.",
    ReasonCode.APPROVAL_REJECTED: "A human reviewer rejected the approval.",
    ReasonCode.APPROVER_NOT_COMPLIANCE_OFFICER: "The approver is not a Compliance Officer.",
}


def explain(result: RequirementResult) -> str:
    text = _REASONS[result.reason]
    if result.reason is ReasonCode.REPORT_OLDER_THAN_MAX_AGE and result.period_end:
        text += f" Audit period ended {result.period_end.isoformat()}."
    return text
