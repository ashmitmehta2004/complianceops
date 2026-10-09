"""The execution boundary.

tool request -> lookup -> permission check -> argument validation -> handler -> output check.

Every outcome, including every failure, is returned as a ToolResult; nothing is raised to the
caller. The runtime owns database sessions, so callers (and the LLM) never touch the database.
"""

import logging

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.tools.base import ToolContext, ToolError, ToolErrorCode, ToolFailure, ToolResult
from app.tools.permissions import Principal
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _failure(tool_name: str, code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(tool_name=tool_name, error=ToolError(code=code, message=message))


def _describe(exc: ValidationError) -> str:
    # Field paths and messages only: never echo the rejected input values.
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or 'arguments'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    )


class ToolRuntime:
    def __init__(self, registry: ToolRegistry, session_factory: sessionmaker[Session]) -> None:
        self._registry = registry
        self._session_factory = session_factory

    def execute(self, tool_name: str, raw_args: object, principal: Principal) -> ToolResult:
        tool = self._registry.get(tool_name)
        if tool is None:
            return _failure(tool_name, ToolErrorCode.UNKNOWN_TOOL, f"unknown tool: {tool_name}")

        if tool.required_permission not in principal.permissions:
            return _failure(
                tool_name,
                ToolErrorCode.PERMISSION_DENIED,
                f"{principal.name} lacks permission {tool.required_permission}",
            )

        try:
            args = tool.input_model.model_validate(raw_args)
        except ValidationError as exc:
            return _failure(tool_name, ToolErrorCode.INVALID_ARGUMENTS, _describe(exc))

        output: BaseModel
        try:
            # Closing the session discards anything uncommitted; the runtime never commits.
            with self._session_factory() as session:
                output = tool.handler(args, ToolContext(session))
        except ToolFailure as exc:
            return _failure(tool_name, exc.code, exc.message)
        except Exception:
            logger.exception("tool %s failed unexpectedly", tool_name)
            return _failure(tool_name, ToolErrorCode.EXECUTION_ERROR, "tool execution failed")

        if not isinstance(output, tool.output_model):
            logger.error("tool %s returned %s", tool_name, type(output).__name__)
            return _failure(
                tool_name, ToolErrorCode.INVALID_OUTPUT, "tool returned an invalid result"
            )
        return ToolResult(tool_name=tool_name, output=output)
