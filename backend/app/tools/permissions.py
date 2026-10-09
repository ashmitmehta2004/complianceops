"""Permissions a caller may hold. Enforced by the tool runtime, never by the LLM."""

from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    VENDOR_READ = "vendor:read"
    POLICY_READ = "policy:read"
    EVIDENCE_READ = "evidence:read"
    DOCUMENT_READ = "document:read"
    COMPLIANCE_EVALUATE = "compliance:evaluate"


@dataclass(frozen=True)
class Principal:
    """Who is calling a tool and exactly what they are allowed to do."""

    name: str
    permissions: frozenset[Permission] = frozenset()
