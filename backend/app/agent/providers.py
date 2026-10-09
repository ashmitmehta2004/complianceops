"""Select the LLM provider from the environment."""

import os

from app.agent.llm import AnthropicLLM, LLMClient, LLMError

PROVIDERS = ("anthropic", "gemini")


def llm_from_env() -> LLMClient:
    """Build the configured provider. Raises LLMError (safe, names only env vars) when the
    provider is unknown or its credentials/model are missing. There is no silent fallback."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicLLM()
    if provider == "gemini":
        from app.agent.gemini import GeminiLLM  # imported lazily: only needed for Gemini

        return GeminiLLM()
    raise LLMError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)}")
