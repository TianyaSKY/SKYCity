"""The OpenAI provider must expose every tool the decision loop can execute.

Regression: M5 economy tools (work / buy_item / sell_item / use_item) were
implemented and routed in DecisionService._execute_tool but never registered
on the SDK Agent — the model could only ever choose move/wait/talk, so agents
talked about earning money ("去农场找零工") but never worked or shopped.
"""

from __future__ import annotations

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
