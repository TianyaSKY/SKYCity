"""Real LLM decision provider built on the openai-agents SDK.

One ``Runner.run`` per decision with ``tool_choice="required"`` and
``parallel_tool_calls=False`` (docs/agent-prompt.md §2), capped at 4 turns so
the agent cannot loop on tools forever. The first function call in
``result.new_items`` is extracted; with tool_choice=required the model must
produce one, so a missing call is a hard DecisionError.

Requires OPENAI_API_KEY at construction; without it the runtime selects the
fake provider instead (providers/__init__.py).
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Any

from loguru import logger
from agents import Agent, ModelSettings, RunContextWrapper, Runner
from agents.items import ToolCallItem

from app.agents.context import AgentToolContext
from app.agents.instructions import build_system_prompt
from app.agents.providers.base import DecisionError, DecisionResult
from app.agents.tools.movement import move, wait
from app.config.settings import Settings, get_settings

MAX_TURNS = 4


class OpenAIProvider:
    """DecisionProvider backed by the OpenAI Agents SDK."""

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise DecisionError(
                "OPENAI_API_KEY is not set: cannot instantiate the OpenAI provider. "
                "Set OPENAI_API_KEY or use llm_provider='fake'."
            )
        self._settings = settings
        self._tools = [move, wait]

    # ------------------------------------------------------------------ #
    # DecisionProvider
    # ------------------------------------------------------------------ #

    async def decide(
        self,
        *,
        observation: str,
        context: AgentToolContext,
        trace_id: str,
    ) -> DecisionResult:
        started = time.perf_counter()
        agent = Agent(
            name=self._agent_name(context.agent_id),
            instructions=self._system_prompt(context.agent_id),
            tools=self._tools,
            model=self._settings.llm_model,
            model_settings=ModelSettings(
                tool_choice="required",
                parallel_tool_calls=False,
            ),
        )
        result = await Runner.run(
            agent,
            observation,
            context=RunContextWrapper(context),
            max_turns=MAX_TURNS,
        )

        tool_call = self._first_tool_call(result.new_items)
        if tool_call is None:
            raise DecisionError(
                f"no tool call in LLM run (trace_id={trace_id}); "
                "expected tool_choice=required to force one"
            )
        tool_name = str(tool_call.tool_name or "")
        try:
            arguments = json.loads(tool_call.raw_item.arguments or "{}")
        except (AttributeError, json.JSONDecodeError) as exc:
            raise DecisionError(f"unparseable tool arguments for {tool_name}: {exc}") from exc
        if not isinstance(arguments, dict):
            arguments = {}

        latency_ms = max(int((time.perf_counter() - started) * 1000), 1)
        input_tokens = sum(
            getattr(resp.usage, "input_tokens", 0) or 0 for resp in result.raw_responses
        )
        output_tokens = sum(
            getattr(resp.usage, "output_tokens", 0) or 0 for resp in result.raw_responses
        )
        return DecisionResult(
            tool_name=tool_name,
            tool_arguments=arguments,
            model=self._settings.llm_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            raw_summary=f"[openai:{self._settings.llm_model}] {tool_name} {arguments}",
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _first_tool_call(new_items: list[Any]) -> ToolCallItem | None:
        """First function/tool call in the run's new items (SDK 0.19: ToolCallItem)."""
        for item in new_items:
            if isinstance(item, ToolCallItem) and item.tool_name:
                return item
        return None

    @staticmethod
    @lru_cache(maxsize=64)
    def _system_prompt(agent_id: str) -> str:
        """Static per-agent prompt, cached by agent_id (identity never changes)."""
        identity = OpenAIProvider._load_identity(agent_id)
        return build_system_prompt(identity)

    @staticmethod
    def _load_identity(agent_id: str) -> dict[str, Any]:
        path = (
            get_settings().world_data_dir / "identities" / f"{agent_id}.json"
        )
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("Identity card unavailable for {}: {}", agent_id, exc)
            return {"name": agent_id}

    def _agent_name(self, agent_id: str) -> str:
        return self._load_identity(agent_id).get("name") or agent_id
