import pytest
from pydantic import BaseModel

from app.tools.base import ToolContext, ToolDefinition, ToolInput
from app.tools.permissions import Permission
from app.tools.readonly import build_default_registry
from app.tools.registry import ToolRegistry


class Empty(ToolInput):
    pass


class Nothing(BaseModel):
    pass


def make_tool(name: str = "noop") -> ToolDefinition:
    def handler(args: Empty, ctx: ToolContext) -> Nothing:
        return Nothing()

    return ToolDefinition(
        name=name,
        description="does nothing",
        input_model=Empty,
        output_model=Nothing,
        required_permission=Permission.VENDOR_READ,
        handler=handler,
    )


def test_lookup_returns_registered_tool() -> None:
    registry = ToolRegistry()
    tool = make_tool()
    registry.register(tool)

    assert registry.get("noop") is tool


def test_lookup_of_unknown_tool_returns_none() -> None:
    assert ToolRegistry().get("missing") is None


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(make_tool())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_tool())


@pytest.mark.parametrize("name", ["", "Get_Vendor", "get-vendor", "1tool", "get vendor"])
def test_invalid_tool_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="invalid tool name"):
        make_tool(name)


def test_default_registry_exposes_the_read_only_tools() -> None:
    registry = build_default_registry()

    assert [tool.name for tool in registry.list()] == [
        "evaluate_vendor_compliance",
        "get_policy",
        "get_vendor",
        "read_document",
        "search_evidence",
    ]


def test_every_default_tool_declares_description_schemas_and_permission() -> None:
    for tool in build_default_registry().list():
        assert tool.description
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["type"] == "object"
        assert isinstance(tool.required_permission, Permission)
