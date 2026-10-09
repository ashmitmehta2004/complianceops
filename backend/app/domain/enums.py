"""Typed states for ComplianceOps entities.

Only the vocabulary lives here; transition rules are enforced by later layers.
"""

from enum import StrEnum


class WorkflowStatus(StrEnum):
    """Lifecycle shared by compliance reviews and agent runs.

    COMPLETED is the successful terminal state, FAILED the unsuccessful one, and
    WAITING_APPROVAL is the persisted human-approval boundary.
    """

    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"

    @property
    def is_terminal(self) -> bool:
        return self in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED}


class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EvidenceStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    MISSING = "MISSING"
    PENDING = "PENDING"


class ToolCallStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class RunOutcome(StrEnum):
    """Why a run ended (or paused). `status` says where the workflow is; `outcome` says why."""

    COMPLIANT_APPROVED = "COMPLIANT_APPROVED"  # evidence valid and officer approval recorded
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # evidence valid, human approval pending
    EVIDENCE_DEFICIENT = "EVIDENCE_DEFICIENT"  # vendor/human must supply or fix evidence
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    LIMIT_REACHED = "LIMIT_REACHED"
    MODEL_ERROR = "MODEL_ERROR"
    INVALID_TARGET = "INVALID_TARGET"  # goal did not identify exactly one known vendor
    INTERRUPTED = "INTERRUPTED"  # process stopped mid-run; recovered as FAILED on startup
