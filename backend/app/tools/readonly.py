"""Deterministic, read-only tools over vendors, policies, evidence and documents."""

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select

from app.domain.enums import EvidenceStatus
from app.persistence.models import ComplianceReview, Document, EvidenceCheck, Policy, Vendor
from app.tools.base import ToolContext, ToolDefinition, ToolErrorCode, ToolFailure, ToolInput
from app.tools.compliance import register_compliance_tools
from app.tools.permissions import Permission
from app.tools.registry import ToolRegistry


def _not_found(what: str) -> ToolFailure:
    return ToolFailure(ToolErrorCode.NOT_FOUND, f"{what} not found")


# get_vendor
class GetVendorInput(ToolInput):
    vendor_id: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> Self:
        if (self.vendor_id is None) == (self.name is None):
            raise ValueError("provide exactly one of vendor_id or name")
        return self


class VendorOutput(BaseModel):
    id: str
    name: str
    metadata: dict[str, Any]
    created_at: datetime


def get_vendor(args: GetVendorInput, ctx: ToolContext) -> VendorOutput:
    if args.vendor_id is not None:
        stmt = select(Vendor).where(Vendor.id == args.vendor_id)
    else:
        stmt = select(Vendor).where(Vendor.name == args.name)
    vendor = ctx.session.scalars(stmt).one_or_none()
    if vendor is None:
        raise _not_found("vendor")
    return VendorOutput(
        id=vendor.id, name=vendor.name, metadata=vendor.metadata_, created_at=vendor.created_at
    )


# get_policy
class GetPolicyInput(ToolInput):
    policy_id: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    version: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _valid_selector(self) -> Self:
        if (self.policy_id is None) == (self.name is None):
            raise ValueError("provide exactly one of policy_id or name")
        if self.version is not None and self.name is None:
            raise ValueError("version can only be combined with name")
        return self


class PolicyOutput(BaseModel):
    id: str
    name: str
    version: str
    rules: dict[str, Any]
    created_at: datetime


def get_policy(args: GetPolicyInput, ctx: ToolContext) -> PolicyOutput:
    if args.policy_id is not None:
        stmt = select(Policy).where(Policy.id == args.policy_id)
    else:
        stmt = select(Policy).where(Policy.name == args.name)
        if args.version is not None:
            stmt = stmt.where(Policy.version == args.version)
        # Without a version, the most recently created policy of that name wins.
        stmt = stmt.order_by(Policy.created_at.desc(), Policy.id).limit(1)
    policy = ctx.session.scalars(stmt).one_or_none()
    if policy is None:
        raise _not_found("policy")
    return PolicyOutput(
        id=policy.id,
        name=policy.name,
        version=policy.version,
        rules=policy.rules,
        created_at=policy.created_at,
    )


# search_evidence
class SearchEvidenceInput(ToolInput):
    review_id: str = Field(min_length=1)
    status: EvidenceStatus | None = None
    text: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=50, ge=1, le=200)


class EvidenceCheckOutput(BaseModel):
    id: str
    requirement: str
    status: EvidenceStatus
    reason: str | None
    document_id: str | None


class SearchEvidenceOutput(BaseModel):
    review_id: str
    results: list[EvidenceCheckOutput]
    truncated: bool


def search_evidence(args: SearchEvidenceInput, ctx: ToolContext) -> SearchEvidenceOutput:
    if ctx.session.get(ComplianceReview, args.review_id) is None:
        raise _not_found("review")
    stmt = select(EvidenceCheck).where(EvidenceCheck.review_id == args.review_id)
    if args.status is not None:
        stmt = stmt.where(EvidenceCheck.status == args.status)
    if args.text is not None:
        stmt = stmt.where(
            or_(
                EvidenceCheck.requirement.icontains(args.text, autoescape=True),
                EvidenceCheck.reason.icontains(args.text, autoescape=True),
            )
        )
    # Fetch one extra row to learn whether the limit cut results off.
    rows = ctx.session.scalars(
        stmt.order_by(EvidenceCheck.requirement, EvidenceCheck.id).limit(args.limit + 1)
    ).all()
    return SearchEvidenceOutput(
        review_id=args.review_id,
        results=[
            EvidenceCheckOutput(
                id=row.id,
                requirement=row.requirement,
                status=row.status,
                reason=row.reason,
                document_id=row.document_id,
            )
            for row in rows[: args.limit]
        ],
        truncated=len(rows) > args.limit,
    )


# read_document
class ReadDocumentInput(ToolInput):
    document_id: str = Field(min_length=1)
    max_chars: int = Field(default=20_000, ge=1, le=100_000)


class DocumentOutput(BaseModel):
    id: str
    vendor_id: str
    document_type: str
    filename: str
    source: str
    extraction_method: str | None
    content: str
    total_chars: int
    truncated: bool


def read_document(args: ReadDocumentInput, ctx: ToolContext) -> DocumentOutput:
    document = ctx.session.get(Document, args.document_id)
    if document is None:
        raise _not_found("document")
    return DocumentOutput(
        id=document.id,
        vendor_id=document.vendor_id,
        document_type=document.document_type,
        filename=document.filename,
        source=document.source,
        extraction_method=document.extraction_method,
        content=document.content[: args.max_chars],
        total_chars=len(document.content),
        truncated=len(document.content) > args.max_chars,
    )


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_compliance_tools(registry)
    registry.register(
        ToolDefinition(
            name="get_vendor",
            description="Fetch a vendor by id or exact name.",
            input_model=GetVendorInput,
            output_model=VendorOutput,
            required_permission=Permission.VENDOR_READ,
            handler=get_vendor,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_policy",
            description=(
                "Fetch a compliance policy by id, or by name (optionally a specific version; "
                "otherwise the newest version)."
            ),
            input_model=GetPolicyInput,
            output_model=PolicyOutput,
            required_permission=Permission.POLICY_READ,
            handler=get_policy,
        )
    )
    registry.register(
        ToolDefinition(
            name="search_evidence",
            description=(
                "List evidence checks of one compliance review, optionally filtered by status "
                "or by case-insensitive text in the requirement or reason."
            ),
            input_model=SearchEvidenceInput,
            output_model=SearchEvidenceOutput,
            required_permission=Permission.EVIDENCE_READ,
            handler=search_evidence,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_document",
            description="Read a document's metadata and text content (truncated to max_chars).",
            input_model=ReadDocumentInput,
            output_model=DocumentOutput,
            required_permission=Permission.DOCUMENT_READ,
            handler=read_document,
        )
    )
    return registry
