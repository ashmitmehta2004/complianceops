"""One real LLM request: `python -m app.llm_smoke`.

Uses the provider configured in the environment/.env and the real tool schemas, and asks the
model to call a tool. Prints only the provider, stop reason, requested tool calls and text;
never the API key. Costs one API request.
"""

import os
import sys

from app.agent.llm import LLMError
from app.agent.providers import llm_from_env
from app.tools.readonly import build_default_registry


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()  # picks up .env from the working directory, if present
    except ImportError:
        pass
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    tools = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in build_default_registry().list()
    ]
    try:
        llm = llm_from_env()
        response = llm.create_message(
            system="You are a test harness. Use the provided tools when asked.",
            messages=[{"role": "user", "content": "Look up the vendor named 'Acme Corp'."}],
            tools=tools,
        )
    except LLMError as exc:
        print(f"FAILED ({provider}): {exc}")
        return 1
    print(f"provider={provider} stop_reason={response.stop_reason}")
    for use in response.tool_uses:
        print(f"tool_call: {use.name} {use.input}")
    if response.text:
        print(f"text: {response.text}")
    ok = response.stop_reason in {"tool_use", "end_turn"}
    print("OK" if ok else "UNEXPECTED stop reason")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
