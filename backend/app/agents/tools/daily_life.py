"""use_item / sleep tools for LLM agents (M5 economy, daily life).

use_item funnels through EconomyService's use_item — only food
(satiety_restore > 0) is consumable, and exactly one unit is consumed per call.
sleep funnels through ActionExecutionService (R1: interruptible like wait).
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext
from app.config.gameplay import (
    HOTEL_NIGHTLY_FEE,
    SLEEP_ENERGY_PER_HOUR,
    SLEEP_MAX_MINUTES,
    SLEEP_MIN_MINUTES,
    SLEEP_MOOD_PER_HOUR,
)

SLEEP_TOOL_DESCRIPTION = (
    "睡觉恢复精力和心情（比 wait 快得多，每小时 +"
    f"{SLEEP_ENERGY_PER_HOUR} 精力 / +{SLEEP_MOOD_PER_HOUR} 心情）："
    f"minutes {SLEEP_MIN_MINUTES}~{SLEEP_MAX_MINUTES}，建议深夜或精力低时使用。"
    "有家必须在家睡，无家必须去小镇旅店(village_hotel)睡"
    f"（每晚 {HOTEL_NIGHTLY_FEE} 金币）。睡觉与 wait 一样可被打断。"
)


@function_tool
async def use_item(
        ctx: RunContextWrapper[AgentToolContext],
        item_id: str,
        reason: str,
) -> str:
    """食用/使用背包中的一件物品（只有食物能提高饱食度，每次消耗 1 件）。"""
    service = ctx.context.engine.economy_service
    if service is None:
        return json.dumps({"success": False, "reason": "经济服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.use_item(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        item_id=item_id,
        reason=reason,
    )
    return json.dumps(
        {
            "success": ok,
            "reason": err,
            "event": envelope.model_dump() if envelope is not None else None,
        },
        ensure_ascii=False,
    )


@function_tool(description_override=SLEEP_TOOL_DESCRIPTION, use_docstring_info=False)
async def sleep(
        ctx: RunContextWrapper[AgentToolContext],
        minutes: int,
        reason: str,
) -> str:
    """睡觉恢复精力和心情；数值见 SLEEP_TOOL_DESCRIPTION（配置驱动）。"""
    minutes = max(SLEEP_MIN_MINUTES, min(int(minutes), SLEEP_MAX_MINUTES))
    ok, envelope, err = ctx.context.action_service.execute_sleep(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        minutes=minutes,
        reason=reason,
        trace_id=ctx.context.trace_id,
    )
    return json.dumps(
        {
            "success": ok,
            "reason": err,
            "event": envelope.model_dump() if envelope is not None else None,
        },
        ensure_ascii=False,
    )
