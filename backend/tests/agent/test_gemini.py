import base64
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors, types
from sqlalchemy.orm import Session, sessionmaker

from app.agent.gemini import DEFAULT_GEMINI_MODEL, GeminiLLM
from app.agent.llm import AnthropicLLM, LLMError
from app.agent.providers import llm_from_env
from app.agent.runtime import AgentLimits, AgentRuntime, RunOutcome
from app.domain.enums import WorkflowStatus
from app.persistence.models import Vendor

TOOLS = [
    {
        "name": "get_vendor",
        "description": "Fetch a vendor.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
    }
]


class FakeModels:
    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_llm(*script: Any) -> tuple[GeminiLLM, FakeModels]:
    models = FakeModels(*script)
    return GeminiLLM(model="test-model", client=SimpleNamespace(models=models)), models


def reply(
    *parts: types.Part, finish: types.FinishReason = types.FinishReason.STOP
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)), finish_reason=finish
            )
        ]
    )


def call_part(
    name: str, args: dict[str, Any] | None, id_: str | None = "c1", sig: bytes | None = None
) -> types.Part:
    return types.Part(
        function_call=types.FunctionCall(id=id_, name=name, args=args), thought_signature=sig
    )


def ask(llm: GeminiLLM, messages: list[dict[str, Any]] | None = None) -> Any:
    return llm.create_message(
        system="sys", messages=messages or [{"role": "user", "content": "hi"}], tools=TOOLS
    )


# ---- responses ----


def test_text_response_is_end_turn() -> None:
    llm, models = make_llm(reply(types.Part(text="Looks fine.")))
    out = ask(llm)
    assert out.stop_reason == "end_turn" and out.text == "Looks fine." and out.tool_uses == []
    call = models.calls[0]
    assert call["model"] == "test-model"
    config = call["config"]
    assert config.system_instruction == "sys"
    assert config.automatic_function_calling.disable is True  # SDK must never run tools itself
    decl = config.tools[0].function_declarations[0]
    assert decl.name == "get_vendor" and decl.parameters_json_schema == TOOLS[0]["input_schema"]


def test_single_function_call_maps_to_tool_use() -> None:
    llm, _ = make_llm(reply(call_part("get_vendor", {"name": "Acme"}, sig=b"\x01\x02")))
    out = ask(llm)
    assert out.stop_reason == "tool_use"
    assert [(u.id, u.name, u.input) for u in out.tool_uses] == [
        ("c1", "get_vendor", {"name": "Acme"})
    ]
    assert out.content[0]["thought_signature"] == base64.b64encode(b"\x01\x02").decode()


def test_multiple_function_calls_and_missing_ids() -> None:
    llm, _ = make_llm(
        reply(
            call_part("get_vendor", {"name": "A"}, id_=None), call_part("get_vendor", None, id_="x")
        )
    )
    out = ask(llm)
    assert [u.name for u in out.tool_uses] == ["get_vendor", "get_vendor"]
    assert out.tool_uses[0].id.startswith("local-call-") and out.tool_uses[1].id == "x"
    assert out.tool_uses[1].input == {}


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        (types.FinishReason.MAX_TOKENS, "max_tokens"),
        (types.FinishReason.SAFETY, "safety"),
        (types.FinishReason.MALFORMED_FUNCTION_CALL, "malformed_function_call"),
    ],
)
def test_abnormal_finish_is_not_end_turn_and_drops_tool_calls(
    finish: types.FinishReason, expected: str
) -> None:
    llm, _ = make_llm(reply(call_part("get_vendor", {}), finish=finish))
    out = ask(llm)
    assert out.stop_reason == expected
    assert out.tool_uses == []  # never execute calls from an unfinished response


def test_blocked_prompt_has_no_candidates() -> None:
    blocked = types.GenerateContentResponse(
        candidates=None,
        prompt_feedback=types.GenerateContentResponsePromptFeedback(
            block_reason=types.BlockedReason.SAFETY
        ),
    )
    out = ask(make_llm(blocked)[0])
    assert out.stop_reason == "blocked:safety"


@pytest.mark.parametrize(
    "bad",
    [
        reply(),  # empty completion with STOP
        reply(call_part("", {})),  # function call without a name
        reply(types.Part(inline_data=types.Blob(data=b"x", mime_type="image/png"))),
    ],
)
def test_malformed_responses_raise_instead_of_succeeding(bad: Any) -> None:
    with pytest.raises(LLMError):
        ask(make_llm(bad)[0])


# ---- conversation conversion ----


def test_history_conversion_pairs_function_responses_with_calls() -> None:
    llm, models = make_llm(reply(types.Part(text="done")))
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "goal"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "get_vendor",
                    "input": {"name": "A"},
                    "thought_signature": base64.b64encode(b"sig").decode(),
                },
                {"type": "tool_use", "id": "local-call-2", "name": "get_vendor", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": '{"id": "v1"}'},
                {
                    "type": "tool_result",
                    "tool_use_id": "local-call-2",
                    "is_error": True,
                    "content": '{"error": {"code": "NOT_FOUND"}}',
                },
            ],
        },
    ]
    ask(llm, messages)
    contents = models.calls[0]["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    model_parts = contents[1].parts
    assert model_parts[1].function_call.id == "c1" and model_parts[1].thought_signature == b"sig"
    assert model_parts[2].function_call.id is None  # invented ids are not sent to Gemini
    ok, err = (p.function_response for p in contents[2].parts)
    assert (ok.id, ok.name, ok.response) == ("c1", "get_vendor", {"output": {"id": "v1"}})
    assert err.id is None and err.name == "get_vendor"
    assert err.response == {"error": {"error": {"code": "NOT_FOUND"}}}


def test_orphan_tool_result_is_rejected() -> None:
    llm, _ = make_llm()
    bad = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "zz", "content": "{}"}]}
    ]
    with pytest.raises(LLMError):
        ask(llm, bad)


# ---- errors and configuration ----


def test_api_error_is_sanitized() -> None:
    secret = "AIza-super-secret-key"
    err = errors.ClientError(429, {"error": {"message": f"quota exceeded for key {secret}"}})
    with pytest.raises(LLMError) as info:
        ask(make_llm(err)[0])
    assert str(info.value) == "ClientError (HTTP 429)"
    assert secret not in str(info.value) and secret not in repr(info.value)


def test_network_error_is_wrapped() -> None:
    with pytest.raises(LLMError, match="ConnectError"):
        ask(make_llm(httpx.ConnectError("boom with secret"))[0])


def test_missing_api_key_and_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLM()
    assert GeminiLLM(client=object())._model == DEFAULT_GEMINI_MODEL
    monkeypatch.setenv("GEMINI_MODEL", "custom-model")
    assert GeminiLLM(client=object())._model == "custom-model"


# ---- provider selection ----


def test_provider_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LLM_PROVIDER", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(LLMError, match="ANTHROPIC_MODEL"):  # default provider, unconfigured
        llm_from_env()

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):  # no fallback to anthropic
        llm_from_env()
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    assert isinstance(llm_from_env(), GeminiLLM)

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert isinstance(llm_from_env(), AnthropicLLM)

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(LLMError, match="LLM_PROVIDER"):
        llm_from_env()


# ---- through the real runtime ----


def test_multi_turn_run_through_runtime_cannot_be_completed_by_gemini(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    session.add(Vendor(name="Acme Corp"))
    session.commit()
    llm, models = make_llm(
        reply(call_part("get_vendor", {"name": "Acme Corp"}, id_="c1", sig=b"s")),
        reply(call_part("drop_tables", {}, id_="c2")),  # unknown tool: must be refused
        reply(types.Part(text="Vendor is fully compliant and approved.")),
    )
    result = AgentRuntime(llm, session_factory).run(
        "Review Acme Corp's compliance status and prepare it for approval."
    )

    assert result.tool_calls == 2 and result.failed_tool_calls == 1
    # No evidence exists, so Gemini's claim is ignored: verification fails the run.
    assert result.status is WorkflowStatus.FAILED
    assert result.outcome is RunOutcome.EVIDENCE_DEFICIENT
    assert result.model_summary == "Vendor is fully compliant and approved."
    # Third request carries the whole history: goal, call, result, call, error result.
    history = models.calls[2]["contents"]
    assert [c.role for c in history] == ["user", "model", "user", "model", "user"]
    assert history[2].parts[0].function_response.id == "c1"
    assert (
        history[4].parts[0].function_response.response["error"]["error"]["code"] == "UNKNOWN_TOOL"
    )
    assert history[1].parts[0].thought_signature == b"s"


def test_provider_failure_inside_runtime_fails_run_without_leaking(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    session.add(Vendor(name="Acme Corp"))
    session.commit()
    err = errors.ServerError(503, {"error": {"message": "secret detail AIza123"}})
    # 503 is retryable: the runtime makes 1 + max_llm_retries attempts, then fails the run.
    llm, models = make_llm(err, err, err)
    limits = AgentLimits(retry_backoff_seconds=0)
    result = AgentRuntime(llm, session_factory, limits=limits).run("Review Acme Corp now.")
    assert len(models.calls) == 3
    assert result.outcome is RunOutcome.MODEL_ERROR
    assert "AIza123" not in result.summary and "secret detail" not in result.summary
    assert "ServerError (HTTP 503)" in result.summary
