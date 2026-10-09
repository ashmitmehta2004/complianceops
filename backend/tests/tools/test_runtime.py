import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.models import Vendor
from app.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolFailure,
    ToolInput,
    ToolResult,
)
from app.tools.permissions import Permission, Principal
from app.tools.registry import ToolRegistry
from app.tools.runtime import ToolRuntime

READER = Principal("reader", frozenset({Permission.VENDOR_READ}))
NOBODY = Principal("nobody")


class EchoInput(ToolInput):
    text: str
    times: int = 1


class EchoOutput(BaseModel):
    echoed: str


class OtherOutput(BaseModel):
    something: int


class Probe:
    """Handlers for a test registry; records whether the handler body ran."""

    def __init__(self) -> None:
        self.calls = 0

    def echo(self, args: EchoInput, ctx: ToolContext) -> EchoOutput:
        self.calls += 1
        return EchoOutput(echoed=args.text * args.times)

    def fail(self, args: EchoInput, ctx: ToolContext) -> EchoOutput:
        self.calls += 1
        raise ToolFailure(ToolErrorCode.NOT_FOUND, "no such thing")

    def crash(self, args: EchoInput, ctx: ToolContext) -> EchoOutput:
        self.calls += 1
        raise RuntimeError("secret internal detail")

    def wrong_output(self, args: EchoInput, ctx: ToolContext) -> EchoOutput:
        self.calls += 1
        return OtherOutput(something=1)  # type: ignore[return-value]

    def write(self, args: EchoInput, ctx: ToolContext) -> EchoOutput:
        self.calls += 1
        ctx.session.add(Vendor(name="sneaky"))
        ctx.session.flush()
        return EchoOutput(echoed="written")


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def runtime(probe: Probe, session_factory: sessionmaker[Session]) -> ToolRuntime:
    registry = ToolRegistry()
    for name, handler in [
        ("echo", probe.echo),
        ("fail", probe.fail),
        ("crash", probe.crash),
        ("wrong_output", probe.wrong_output),
        ("write", probe.write),
    ]:
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                input_model=EchoInput,
                output_model=EchoOutput,
                required_permission=Permission.VENDOR_READ,
                handler=handler,
            )
        )
    return ToolRuntime(registry, session_factory)


def assert_failed(result: ToolResult, code: ToolErrorCode) -> None:
    assert not result.ok
    assert result.output is None
    assert result.error is not None
    assert result.error.code is code


# successful execution
def test_successful_execution_returns_typed_output(runtime: ToolRuntime, probe: Probe) -> None:
    result = runtime.execute("echo", {"text": "ab", "times": 2}, READER)

    assert result.ok
    assert result.error is None
    assert result.output == EchoOutput(echoed="abab")
    assert probe.calls == 1


def test_result_dump_keeps_output_fields(runtime: ToolRuntime) -> None:
    result = runtime.execute("echo", {"text": "x"}, READER)

    assert result.model_dump()["output"] == {"echoed": "x"}


# registry lookup
def test_unknown_tool_is_a_failure(runtime: ToolRuntime) -> None:
    assert_failed(runtime.execute("missing", {}, READER), ToolErrorCode.UNKNOWN_TOOL)


# permission denial
def test_missing_permission_is_denied_without_running_handler(
    runtime: ToolRuntime, probe: Probe
) -> None:
    result = runtime.execute("echo", {"text": "x"}, NOBODY)

    assert_failed(result, ToolErrorCode.PERMISSION_DENIED)
    assert probe.calls == 0


def test_other_permissions_do_not_grant_access(runtime: ToolRuntime, probe: Probe) -> None:
    principal = Principal("other", frozenset({Permission.DOCUMENT_READ, Permission.POLICY_READ}))

    assert_failed(
        runtime.execute("echo", {"text": "x"}, principal), ToolErrorCode.PERMISSION_DENIED
    )
    assert probe.calls == 0


def test_permission_is_checked_before_arguments(runtime: ToolRuntime) -> None:
    result = runtime.execute("echo", {"bogus": 1}, NOBODY)

    assert_failed(result, ToolErrorCode.PERMISSION_DENIED)


# schema validation
@pytest.mark.parametrize(
    "raw_args",
    [
        {},
        {"text": 5},
        {"text": "x", "times": "many"},
        {"text": "x", "unexpected": True},
        None,
        "text",
        ["x"],
    ],
)
def test_invalid_arguments_are_rejected_without_running_handler(
    runtime: ToolRuntime, probe: Probe, raw_args: object
) -> None:
    result = runtime.execute("echo", raw_args, READER)

    assert_failed(result, ToolErrorCode.INVALID_ARGUMENTS)
    assert probe.calls == 0


def test_validation_message_names_fields_but_not_values(runtime: ToolRuntime) -> None:
    result = runtime.execute("echo", {"text": 5, "times": "SECRET-VALUE"}, READER)

    assert result.error is not None
    assert "times" in result.error.message
    assert "SECRET-VALUE" not in result.error.message


# tool failure
def test_expected_tool_failure_is_explicit(runtime: ToolRuntime) -> None:
    result = runtime.execute("fail", {"text": "x"}, READER)

    assert_failed(result, ToolErrorCode.NOT_FOUND)
    assert result.error is not None
    assert result.error.message == "no such thing"


def test_unexpected_exception_is_a_failure_that_hides_internals(runtime: ToolRuntime) -> None:
    result = runtime.execute("crash", {"text": "x"}, READER)

    assert_failed(result, ToolErrorCode.EXECUTION_ERROR)
    assert result.error is not None
    assert "secret" not in result.error.message


def test_wrong_output_type_is_a_failure(runtime: ToolRuntime) -> None:
    assert_failed(
        runtime.execute("wrong_output", {"text": "x"}, READER), ToolErrorCode.INVALID_OUTPUT
    )


def test_result_must_have_exactly_one_outcome() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ToolResult(tool_name="t")
    with pytest.raises(ValueError, match="exactly one"):
        ToolResult(
            tool_name="t",
            output=EchoOutput(echoed="x"),
            error={"code": ToolErrorCode.NOT_FOUND, "message": "m"},  # type: ignore[arg-type]
        )


# the runtime never commits
def test_runtime_does_not_persist_handler_writes(runtime: ToolRuntime, session: Session) -> None:
    result = runtime.execute("write", {"text": "x"}, READER)

    assert result.ok
    assert session.scalars(select(Vendor)).all() == []
