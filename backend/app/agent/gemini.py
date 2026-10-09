"""Google Gemini adapter (official `google-genai` SDK) behind the provider-neutral LLMClient.

The runtime keeps one neutral conversation format (text / tool_use / tool_result blocks). This
module converts it to Gemini `Content`/`Part` objects and back, so the runtime has no Gemini
specifics. Everything the model returns is untrusted data: function-call arguments are passed on
unvalidated and are checked by ToolRuntime.

Gemini 3 models attach `thought_signature`s to response parts and require them to be sent back
on the following turn, so they are carried through the neutral blocks (base64) untouched.
"""

import base64
import json
import os
from typing import Any

from google import genai
from google.genai import errors, types

from app.agent.llm import LLMError, LLMResponse, ToolUse, is_retryable_status

# Documented as a function-calling-capable stable model; override with GEMINI_MODEL.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

# Ids we invented because the API returned none; they are not sent back to Gemini.
_LOCAL_ID_PREFIX = "local-call-"

# Gemini finish reasons that mean "model finished normally".
_NORMAL_FINISH = {types.FinishReason.STOP, types.FinishReason.FINISH_REASON_UNSPECIFIED}


class GeminiLLM:
    def __init__(
        self, *, model: str | None = None, max_tokens: int = 4096, client: Any = None
    ) -> None:
        self._model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        if client is None:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise LLMError("GEMINI_API_KEY is not set")
            client = genai.Client(api_key=api_key)
        self._client: Any = client
        self._max_tokens = max_tokens

    def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        contents = _to_contents(messages)
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=self._max_tokens,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["name"],
                            description=t["description"],
                            parameters_json_schema=t["input_schema"],
                        )
                        for t in tools
                    ]
                )
            ]
            if tools
            else None,
            # The runtime executes tools (with permissions); the SDK must never do it itself.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except errors.APIError as exc:
            # Class name and HTTP status only: provider messages may echo request details.
            raise LLMError(
                f"{type(exc).__name__} (HTTP {exc.code})",
                retryable=is_retryable_status(exc.code),
            ) from exc
        except Exception as exc:  # network/timeouts/SDK internals: same safe, generic mapping
            raise LLMError(f"{type(exc).__name__} calling Gemini", retryable=True) from exc
        return _from_response(response)


# ---- neutral blocks -> Gemini ----


def _encode_signature(sig: bytes | None) -> str | None:
    return base64.b64encode(sig).decode() if sig else None


def _decode_signature(block: dict[str, Any]) -> bytes | None:
    raw = block.get("thought_signature")
    return base64.b64decode(raw) if raw else None


def _to_contents(messages: list[dict[str, Any]]) -> list[types.Content]:
    contents: list[types.Content] = []
    names_by_id: dict[str, str] = {}  # tool_use id -> function name, to label results
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            contents.append(
                types.Content(
                    role="model" if role == "assistant" else "user",
                    parts=[types.Part(text=content)],
                )
            )
            continue
        parts: list[types.Part] = []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                parts.append(
                    types.Part(text=block["text"], thought_signature=_decode_signature(block))
                )
            elif kind == "tool_use":
                names_by_id[block["id"]] = block["name"]
                provider_id = block["id"]
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            id=None if provider_id.startswith(_LOCAL_ID_PREFIX) else provider_id,
                            name=block["name"],
                            args=block["input"] if isinstance(block["input"], dict) else {},
                        ),
                        thought_signature=_decode_signature(block),
                    )
                )
            elif kind == "tool_result":
                call_id = block["tool_use_id"]
                if call_id not in names_by_id:
                    raise LLMError("tool result does not match any earlier function call")
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=None if call_id.startswith(_LOCAL_ID_PREFIX) else call_id,
                            name=names_by_id[call_id],
                            response=_result_payload(block),
                        )
                    )
                )
            else:
                raise LLMError(f"unsupported conversation block: {kind!r}")
        contents.append(types.Content(role="model" if role == "assistant" else "user", parts=parts))
    return contents


def _result_payload(block: dict[str, Any]) -> dict[str, Any]:
    raw = block["content"]
    try:
        value: Any = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        value = raw
    return {"error": value} if block.get("is_error") else {"output": value}


# ---- Gemini -> neutral response ----


def _from_response(response: Any) -> LLMResponse:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        # No candidate: the prompt itself was blocked (or the API returned nothing usable).
        feedback = getattr(response, "prompt_feedback", None)
        reason = getattr(getattr(feedback, "block_reason", None), "value", None)
        return LLMResponse(
            stop_reason=f"blocked:{reason.lower()}" if reason else "no_candidates", text=""
        )
    candidate = candidates[0]
    finish = candidate.finish_reason

    texts: list[str] = []
    tool_uses: list[ToolUse] = []
    blocks: list[dict[str, Any]] = []
    parts = getattr(candidate.content, "parts", None) or []
    for index, part in enumerate(parts):
        signature = _encode_signature(part.thought_signature)
        extra = {"thought_signature": signature} if signature else {}
        call = part.function_call
        if call is not None:
            if not call.name:
                raise LLMError("malformed Gemini response: function call without a name")
            call_id = call.id or f"{_LOCAL_ID_PREFIX}{index}"
            args: object = call.args if call.args is not None else {}
            tool_uses.append(ToolUse(id=call_id, name=call.name, input=args))
            blocks.append(
                {"type": "tool_use", "id": call_id, "name": call.name, "input": args, **extra}
            )
        elif part.text is not None and not part.thought:
            texts.append(part.text)
            blocks.append({"type": "text", "text": part.text, **extra})
        elif part.text is None:
            raise LLMError("unsupported Gemini response part (neither text nor function call)")

    if tool_uses and finish in _NORMAL_FINISH:
        stop_reason = "tool_use"
    elif finish in _NORMAL_FINISH:
        if not texts:
            raise LLMError("malformed Gemini response: empty completion")
        stop_reason = "end_turn"
    else:
        # MAX_TOKENS, SAFETY, MALFORMED_FUNCTION_CALL, ...: the runtime fails the run on these.
        stop_reason = str(getattr(finish, "value", finish)).lower()
        tool_uses = []  # never execute calls from a response that did not finish cleanly
    return LLMResponse(
        stop_reason=stop_reason, text="\n".join(texts), tool_uses=tool_uses, content=blocks
    )
