"""Provider selection: real LLM when configured, fake otherwise.

``llm_provider``:
- "openai" -> always the OpenAI provider (raises if OPENAI_API_KEY missing).
- "auto"   -> OpenAI provider only when an API key is present, else fake.
- "fake"   -> deterministic scripted provider (tests, demos).
"""

from __future__ import annotations

from app.agents.providers.base import DecisionError, DecisionProvider
from app.agents.providers.fake_provider import FakeDecisionProvider
from app.agents.providers.openai_provider import OpenAIProvider
from app.config.settings import Settings


def get_provider(settings: Settings) -> DecisionProvider:
    """Pick the decision provider for the given settings."""
    provider_name = (settings.llm_provider or "auto").lower()
    if provider_name == "fake":
        return FakeDecisionProvider()
    if provider_name == "openai":
        return OpenAIProvider(settings)
    if provider_name == "auto":
        if settings.openai_api_key:
            return OpenAIProvider(settings)
        return FakeDecisionProvider()
    raise DecisionError(
        f"unknown llm_provider {settings.llm_provider!r}; expected auto|openai|fake"
    )


__all__ = [
    "DecisionError",
    "DecisionProvider",
    "FakeDecisionProvider",
    "OpenAIProvider",
    "get_provider",
]
