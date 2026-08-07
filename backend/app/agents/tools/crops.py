"""plant / harvest tools for LLM agents (M15 farming, R23).

Both funnel through CropService — the farming rule gate. They never touch
SQL/ORM/WS directly and return the same structured JSON the decision service
records in ``llm_runs.tool_result``: ``{"success", "reason", "event"}``.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext


def _result_json(ok: bool, envelope, reason: str | None) -> str:
    return json.dumps(
        {
            "success": ok,
            "reason": reason,
            "event": envelope.model_dump() if envelope is not None else None,
        },
        ensure_ascii=False,
    )


def _service(ctx: RunContextWrapper[AgentToolContext]):
    service = ctx.context.engine.crop_service
    if service is None:
        raise RuntimeError("种植服务未初始化")
    return service


@function_tool
async def plant(
        ctx: RunContextWrapper[AgentToolContext],
        col: int,
        row: int,
        item_id: str,
        reason: str,
) -> str:
    """在农田 (col,row) 格种下一粒种子（item_id 必须是可种植的种子，如
    wheat_seed/carrot_seed/strawberry_seed/flower_seed）。要求：你离目标格 ≤ 3 格、
    目标格在农场农田范围内且未被占用、背包里至少有一粒该种子。种子会按世界时钟
    生长，成熟后可用 harvest 收获。"""
    service = _service(ctx)
    ok, envelope, err = service.plant(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        col=col,
        row=row,
        item_id=item_id,
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def harvest(
        ctx: RunContextWrapper[AgentToolContext],
        col: int,
        row: int,
        reason: str,
) -> str:
    """收获 (col,row) 格已成熟的作物（作物进入最后生长阶段才可收获；
    未成熟会被拒绝）。收获的产物进入背包，可以卖给商店。"""
    service = _service(ctx)
    ok, envelope, err = service.harvest(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        col=col,
        row=row,
        reason=reason,
    )
    return _result_json(ok, envelope, err)
