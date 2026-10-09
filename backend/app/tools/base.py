"""Tool definition and typed execution outcome."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, SerializeAsAny, model_validator
from sqlalchemy.orm import Session

from app.tools.permissions import Permission

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolInput(BaseModel):
    """Base for tool argument models: unknown arguments are rejected."""

    model_config = ConfigDict(extra="forbid")


class ToolErrorCode(StrEnum):
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    NOT_FOUND = "NOT_FOUND"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    INVALID_OUTPUT = "INVALID_OUTPUT"


class ToolError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: ToolErrorCode
    message: str


class ToolFailure(Exception):
    """Raised by a handler for an expected, reportable failure (e.g. NOT_FOUND)."""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolResult(BaseModel):
    """Outcome of one tool call: exactly one of `output` or `error` is set."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    output: SerializeAsAny[BaseModel] | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> Self:
        if (self.output is None) == (self.error is None):
            raise ValueError("a tool result needs exactly one of output or error")
        return self

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class ToolContext:
    """What a handler may use. Only the runtime constructs this; the LLM never sees it."""

    session: Session


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[ToolInput]
    output_model: type[BaseModel]
    required_permission: Permission
    handler: Callable[[Any, ToolContext], BaseModel]

    def __post_init__(self) -> None:
        if not _TOOL_NAME.match(self.name):
            raise ValueError(f"invalid tool name: {self.name!r}")

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()
