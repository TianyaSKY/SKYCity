"""The OpenAI provider must expose every tool the decision loop can execute.

Regression: M5 economy tools (work / buy_item / sell_item / use_item) were
implemented and routed in DecisionService._execute_tool but never registered
on the SDK Agent — the model could only ever choose move/wait/talk, so agents
talked about earning money ("去农场找零工") but never worked or shopped.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.providers.openai_provider import OpenAIProvider
from app.config.settings import Settings

ALL_TOOLS = {
    "move",
    "wait",
    "talk",
    "work",
    "buy_item",
    "sell_item",
    "use_item",
    "sleep",
    "buy_stock",
    "sell_stock",
    "transfer_money",
    "give_item",
    "build",
    "plant",
    "harvest",
    "apply_job",
    "withdraw_job_application",
    "review_job_application",
    "start_shift",
    "resign_job",
    "request_leave",
    "review_leave_request",
    "terminate_employment",
    "pause_recruitment",
    "resume_recruitment",
    "purchase_company_goods",
    "stock_store",
    # M18: personal shops.
    "open_shop",
    "stock_shop",
    "adjust_price",
    "close_shop",
}


def _provider() -> OpenAIProvider:
    # Construction needs no network; decide() is never called here.
    return OpenAIProvider(Settings(openai_api_key="sk-dummy", llm_provider="openai"))


def test_openai_provider_registers_all_decision_tools() -> None:
    registered = {tool.name for tool in _provider()._tools}
    assert registered == ALL_TOOLS, f"missing tools: {sorted(ALL_TOOLS - registered)}"


def test_economy_tools_are_function_tools() -> None:
    # Guard against a silent rename that keeps names but breaks the SDK call.
    for tool in _provider()._tools:
        assert hasattr(tool, "name") and hasattr(tool, "on_invoke_tool"), (
            f"{tool} is not an SDK function tool"
        )


def test_llm_tool_choice_defaults_to_required() -> None:
    # "required" preserves the one-action-per-decision contract; the provider
    # treats a missing tool call as a hard DecisionError in that mode.
    # _env_file=None: the dev .env may legitimately set LLM_TOOL_CHOICE=auto.
    settings = Settings(_env_file=None)
    assert settings.llm_tool_choice == "required"
    provider = OpenAIProvider(
        Settings(_env_file=None, openai_api_key="sk-dummy", llm_provider="openai")
    )
    assert provider._settings.llm_tool_choice == "required"


def test_llm_tool_choice_accepts_auto_for_thinking_models() -> None:
    # Reasoning models (DeepSeek v4, qwen3.8-max) reject the forced choice;
    # "auto" lets them reply in text, which the provider degrades to wait.
    settings = Settings(llm_tool_choice="auto")
    assert settings.llm_tool_choice == "auto"
    provider = OpenAIProvider(
        Settings(openai_api_key="sk-dummy", llm_provider="openai", llm_tool_choice="auto")
    )
    assert provider._settings.llm_tool_choice == "auto"


def test_llm_tool_choice_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_tool_choice="always")
