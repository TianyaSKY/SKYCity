"""move / wait tools for LLM agents.

Both tools funnel through ActionExecutionService — the single world-rule gate
(R1/R6/R8/R15). They never touch SQL, ORM, WebSockets, or relationships, and
they return the same structured JSON the decision service records in
``llm_runs.tool_result``.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext
from app.config.gameplay import WAIT_MAX_MINUTES, WAIT_MIN_MINUTES


def _result_json(ok: bool, envelope, reason: str | None) -> str:
    """Structured, Chinese-readable tool result for the observation."""
    return json.dumps(
        {
            "success": ok,
            "reason": reason,
            "event": envelope.model_dump() if envelope is not None else None,
        },
        ensure_ascii=False,
    )


@function_tool
async def move(
        ctx: RunContextWrapper[AgentToolContext],
        destination_id: str,
        reason: str,
) -> str:
    """移动到地图上的一个地点（destination_id 必须是可见地点列表中的 id）。"""
    ok, envelope, err = ctx.context.action_service.execute_move(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        destination_id=destination_id,
        reason=reason,
        trace_id=ctx.context.trace_id,
    )
    return _result_json(ok, envelope, err)


@function_tool(
    description_override=f"原地等待 minutes 分钟（{WAIT_MIN_MINUTES}~{WAIT_MAX_MINUTES}），可被移动打断。",
    use_docstring_info=False,
)
async def wait(
        ctx: RunContextWrapper[AgentToolContext],
        minutes: int,
        reason: str,
) -> str:
    """原地等待；数值见 description_override（配置驱动）。"""
    minutes = max(WAIT_MIN_MINUTES, min(int(minutes), WAIT_MAX_MINUTES))
    ok, envelope, err = ctx.context.action_service.execute_wait(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        minutes=minutes,
        reason=reason,
        trace_id=ctx.context.trace_id,
    )
    return _result_json(ok, envelope, err)
