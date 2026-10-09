from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from app.agent.llm import AnthropicLLM, LLMError


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response, self.error, self.kwargs = response, error, {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def test_parses_text_and_tool_use_blocks() -> None:
    response = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="text", text="checking"),
            SimpleNamespace(type="tool_use", id="tu_1", name="get_vendor", input={"name": "A"}),
        ],
    )
    messages = FakeMessages(response)
    llm = AnthropicLLM(model="m", client=SimpleNamespace(messages=messages))
    out = llm.create_message(system="s", messages=[], tools=[])

    assert messages.kwargs["model"] == "m"
    assert out.stop_reason == "tool_use" and out.text == "checking"
    assert out.tool_uses[0].id == "tu_1" and out.tool_uses[0].input == {"name": "A"}
    assert out.content[1] == {
        "type": "tool_use",
        "id": "tu_1",
        "name": "get_vendor",
        "input": {"name": "A"},
    }


def test_api_errors_become_llm_error() -> None:
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    llm = AnthropicLLM(model="m", client=SimpleNamespace(messages=FakeMessages(error=err)))
    with pytest.raises(LLMError):
        llm.create_message(system="s", messages=[], tools=[])


def test_model_must_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    with pytest.raises(LLMError):
        AnthropicLLM(client=object())
