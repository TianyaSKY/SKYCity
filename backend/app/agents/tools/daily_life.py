"""use_item tool for LLM agents (M5 economy, daily life).

Funnels through EconomyService's use_item — only food (hunger_restore > 0)
is consumable, and exactly one unit is consumed per call.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext


@function_tool
async def use_item(
    ctx: RunContextWrapper[AgentToolContext],
    item_id: str,
    reason: str,
) -> str:
    """食用/使用背包中的一件物品（只有食物能恢复饥饿，每次消耗 1 件）。"""
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
