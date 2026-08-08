"""Real LLM decision provider built on the openai-agents SDK.

One ``Runner.run`` per decision with ``parallel_tool_calls=False`` and
``tool_use_behavior="stop_on_first_tool"`` (docs/agent-prompt.md §2): the run
ends as soon as the first tool executes, so exactly one world action per
decision regardless of model turn discipline. ``tool_choice`` comes from
settings: the default ``required`` forces a tool call (a missing one is a hard
DecisionError); ``auto``/``none`` support reasoning models that reject the
forced choice — a text-only reply then degrades into a wait action.
max_turns=4 remains as a safety net for malformed tool calls. The first
function call in ``result.new_items`` is extracted.

Requires OPENAI_API_KEY at construction; without it the runtime selects the
fake provider instead (providers/__init__.py).
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Any

from agents import Agent, ModelSettings, OpenAIProvider as SdkOpenAIProvider, RunContextWrapper, Runner
from agents.items import ToolCallItem
from loguru import logger
from openai.types.shared.reasoning import Reasoning

from app.agents.context import AgentToolContext
from app.agents.instructions import build_system_prompt
from app.agents.providers.base import DecisionError, DecisionResult
from app.agents.tools.build import build
from app.agents.tools.commerce import buy_item, sell_item, work
from app.agents.tools.conversation import talk
from app.agents.tools.crops import harvest, plant
from app.agents.tools.daily_life import sleep, use_item
from app.agents.tools.entrepreneurship import adjust_price, close_shop, open_shop, stock_shop
from app.agents.tools.employment import (
    apply_job,
    pause_recruitment,
    purchase_company_goods,
    request_leave,
    resign_job,
    resume_recruitment,
    review_job_application,
    review_leave_request,
    start_shift,
    stock_store,
    terminate_employment,
    withdraw_job_application,
)
from app.agents.tools.movement import move, wait
from app.agents.tools.stocks import buy_stock, sell_stock
from app.agents.tools.transfers import give_item, transfer_money
from app.config.gameplay import OPENAI_SDK_MAX_TURNS
from app.config.settings import Settings, get_settings


class OpenAIProvider:
    """DecisionProvider backed by the OpenAI Agents SDK."""

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise DecisionError(
                "OPENAI_API_KEY is not set: cannot instantiate the OpenAI provider. "
                "Set OPENAI_API_KEY or use llm_provider='fake'."
            )
        self._settings = settings
        self._tools = [
            move, wait, talk, work, buy_item, sell_item, use_item, sleep,
            buy_stock, sell_stock, transfer_money, give_item, build, plant, harvest,
            apply_job, withdraw_job_application, review_job_application,
            start_shift, resign_job, request_leave, review_leave_request,
            terminate_employment, pause_recruitment, resume_recruitment,
            purchase_company_goods, stock_store,
            open_shop, stock_shop, adjust_price, close_shop,
        ]
        # Explicit SDK provider: routes through settings (base_url / key) and
        # uses chat completions so third-party OpenAI-compatible APIs work.
        self._sdk_provider = SdkOpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            use_responses=settings.llm_use_responses,
        )

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
            model=self._sdk_provider.get_model(self._settings.llm_model),
            # One decision = one world action: end the run as soon as the
            # first tool has executed (agent-prompt.md §2). Without this,
            # models that keep calling tools burn max_turns and raise.
            tool_use_behavior="stop_on_first_tool",
            model_settings=ModelSettings(
                tool_choice=self._settings.llm_tool_choice,
                parallel_tool_calls=False,
                reasoning=self._reasoning_settings(),
            ),
        )
        result = await Runner.run(
            agent,
            observation,
            context=RunContextWrapper(context),
            max_turns=OPENAI_SDK_MAX_TURNS,
        )

        tool_call = self._first_tool_call(result.new_items)
        latency_ms = max(int((time.perf_counter() - started) * 1000), 1)
        input_tokens = sum(
            getattr(resp.usage, "input_tokens", 0) or 0 for resp in result.raw_responses
        )
        output_tokens = sum(
            getattr(resp.usage, "output_tokens", 0) or 0 for resp in result.raw_responses
        )
        if tool_call is None:
            # tool_choice="required": the model must produce a tool call, so a
            # missing one is a hard failure (docs/agent-prompt.md §2).
            if self._settings.llm_tool_choice == "required":
                raise DecisionError(
                    f"no tool call in LLM run (trace_id={trace_id}); "
                    "expected tool_choice=required to force one"
                )
            # tool_choice="auto"/"none": reasoning models may answer in text.
            # Degrade into a short wait (tool_output=None: the decision
            # service executes it) so the world keeps ticking without burning
            # the LLM failure backoff on a legitimate text-only reply.
            final_text = (
                (result.final_output or "").strip()[:80]
                if isinstance(result.final_output, str)
                else ""
            )
            reason = final_text or "未调用工具，原地等待"
            return DecisionResult(
                tool_name="wait",
                tool_arguments={"minutes": 15, "reason": reason},
                model=self._settings.llm_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                raw_summary=(
                    f"wait (text-only reply, "
                    f"tool_choice={self._settings.llm_tool_choice})"
                ),
            )
        tool_name = str(tool_call.tool_name or "")
        try:
            arguments = json.loads(tool_call.raw_item.arguments or "{}")
        except (AttributeError, json.JSONDecodeError) as exc:
            raise DecisionError(f"unparseable tool arguments for {tool_name}: {exc}") from exc
        if not isinstance(arguments, dict):
            arguments = {}
        # stop_on_first_tool: the SDK already executed the tool, so its output
        # is the run's final output. The decision service records this instead
        # of re-executing the tool (re-execution would double the action).
        tool_output = result.final_output if isinstance(result.final_output, str) else None
        return DecisionResult(
            tool_name=tool_name,
            tool_arguments=arguments,
            model=self._settings.llm_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            raw_summary=f"{tool_name} {arguments}",
            tool_output=tool_output,
        )

    # ------------------------------------------------------------------ #
    # Daily reflection (M6 T6-6): separate no-tool agent, stronger model
    # ------------------------------------------------------------------ #

    async def reflect(
            self,
            *,
            digest: str,
            context: AgentToolContext | None,
            trace_id: str,
    ) -> str:
        """One short first-person day summary from the digest.

        A separate agent with no tools (the model cannot mutate the world) and
        the stronger ``llm_reflect_model`` setting.
        """
        agent = Agent(
            name="reflection",
            instructions=(
                "你是一位小镇居民，正在回顾自己的一天。根据给出的当日总结，"
                "用中文写一段简短的第一人称反思（50字以内），表达对今天的感受"
                "和对明天的期望。直接输出反思内容，不要任何前缀或引用。"
            ),
            tools=[],
            model=self._sdk_provider.get_model(self._settings.llm_reflect_model),
            model_settings=ModelSettings(reasoning=self._reasoning_settings()),
        )
        result = await Runner.run(agent, digest, max_turns=1)
        return (result.final_output or "").strip()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _reasoning_settings(self) -> Reasoning | None:
        """Reasoning.effort from settings, or None to keep the provider default.

        Chat completions only honors ``effort`` (mode/context need the
        Responses API); the SDK warns and drops the others.
        """
        if not self._settings.llm_reasoning_effort:
            return None
        return Reasoning(effort=self._settings.llm_reasoning_effort)

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
