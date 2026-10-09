"""Types for deterministic compliance evaluation: inputs, policy parameters and results.

Results are structured (statuses and reason codes), never prose.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, computed_field

from app.domain.enums import ApprovalDecision

# Values of SubmittedDocument.document_type that the evaluator understands.
DOC_SOC2_REPORT = "soc2_report"
DOC_DPA = "dpa"
DOC_SECURITY_QUESTIONNAIRE = "security_questionnaire"


class RequirementKey(StrEnum):
    SOC2_TYPE_II = "SOC2_TYPE_II"
    DPA = "DPA"
    SECURITY_QUESTIONNAIRE = "SECURITY_QUESTIONNAIRE"
    COMPLIANCE_OFFICER_APPROVAL = "COMPLIANCE_OFFICER_APPROVAL"


class RequirementStatus(StrEnum):
    VALID = "VALID"  # requirement satisfied
    MISSING = "MISSING"  # nothing submitted for the requirement
    EXPIRED = "EXPIRED"  # right document, too old
    INCOMPLETE = "INCOMPLETE"  # right document, required information absent or not done
    INVALID = "INVALID"  # present but cannot satisfy the requirement


class ReasonCode(StrEnum):
    OK = "OK"
    NO_DOCUMENT = "NO_DOCUMENT"
    MISSING_FACTS = "MISSING_FACTS"
    MALFORMED_FACTS = "MALFORMED_FACTS"
    REPORT_TYPE_NOT_TYPE_II = "REPORT_TYPE_NOT_TYPE_II"
    REPORT_DATE_MISSING = "REPORT_DATE_MISSING"
    REPORT_DATE_IN_FUTURE = "REPORT_DATE_IN_FUTURE"
    REPORT_OLDER_THAN_MAX_AGE = "REPORT_OLDER_THAN_MAX_AGE"
    NOT_SIGNED = "NOT_SIGNED"
    QUESTIONS_UNANSWERED = "QUESTIONS_UNANSWERED"
    NO_APPROVAL = "NO_APPROVAL"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVER_NOT_COMPLIANCE_OFFICER = "APPROVER_NOT_COMPLIANCE_OFFICER"


class SubmittedDocument(BaseModel):
    """A document as the evaluator sees it. `facts` are the structured, already-extracted
    values (the caller maps them from Document.metadata_); prose is never parsed here."""

    model_config = ConfigDict(frozen=True)

    id: str
    document_type: str
    submitted_at: AwareDatetime
    facts: dict[str, Any]


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    decision: ApprovalDecision
    approver_role: str | None
    requested_at: AwareDatetime


@dataclass(frozen=True)
class CompliancePolicy:
    soc2_max_age_months: int = 12
    compliance_officer_role: str = "compliance_officer"


DEFAULT_POLICY = CompliancePolicy()


class RequirementResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementKey
    status: RequirementStatus
    reason: ReasonCode
    source_id: str | None = None  # the document or approval the decision is based on
    period_end: date | None = None  # SOC 2 only
    expires_on: date | None = None  # SOC 2 only: first day the report counts as expired


class ComplianceEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluated_at: AwareDatetime
    results: list[RequirementResult]

    def _all_valid(self, *keys: RequirementKey) -> bool:
        statuses = {r.requirement: r.status for r in self.results}
        return all(statuses.get(key) is RequirementStatus.VALID for key in keys)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_compliant(self) -> bool:
        return self._all_valid(
            RequirementKey.SOC2_TYPE_II, RequirementKey.DPA, RequirementKey.SECURITY_QUESTIONNAIRE
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def approval_satisfied(self) -> bool:
        return self._all_valid(RequirementKey.COMPLIANCE_OFFICER_APPROVAL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def can_finalize(self) -> bool:
        return self.evidence_compliant and self.approval_satisfied
