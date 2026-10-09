"""Thin Anthropic Messages API adapter.

The runtime depends on the `LLMClient` protocol, not on the SDK, so the loop is testable with a
scripted client. Everything the model returns is untrusted data.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic


class LLMError(Exception):
    """Any failure to obtain a usable model response (API error, bad configuration).

    `retryable` marks transient provider failures (rate limit, 5xx, network); the runtime retries
    only those, a bounded number of times."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def is_retryable_status(status: int | None) -> bool:
    return status is not None and (status in (408, 409, 429) or status >= 500)


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: object  # untrusted: validated by ToolRuntime, not here


@dataclass(frozen=True)
class LLMResponse:
    stop_reason: str | None
    text: str
    tool_uses: list[ToolUse] = field(default_factory=list)
    # Assistant content as request-shaped blocks, echoed back verbatim on the next turn.
    content: list[dict[str, Any]] = field(default_factory=list)


class LLMClient(Protocol):
    def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(
        self, *, model: str | None = None, max_tokens: int = 4096, client: Any = None
    ) -> None:
        resolved = model or os.environ.get("ANTHROPIC_MODEL")
        if not resolved:
            raise LLMError("ANTHROPIC_MODEL is not set")
        if client is None and not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._model = resolved
        self._max_tokens = max_tokens
        # Reads ANTHROPIC_API_KEY from the environment when no client is injected.
        self._client: Any = client or anthropic.Anthropic()

    def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )
        except anthropic.APIError as exc:
            # Class name and HTTP status only: SDK messages may echo request details.
            status = getattr(exc, "status_code", None)
            raise LLMError(
                f"{type(exc).__name__}" + (f" (HTTP {status})" if status else ""),
                retryable=is_retryable_status(status) or status is None,
            ) from exc

        texts: list[str] = []
        tool_uses: list[ToolUse] = []
        content: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_uses.append(ToolUse(id=block.id, name=block.name, input=block.input))
                content.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )
        return LLMResponse(
            stop_reason=response.stop_reason,
            text="\n".join(texts),
            tool_uses=tool_uses,
            content=content,
        )
