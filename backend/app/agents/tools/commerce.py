"""work / buy_item / sell_item tools for LLM agents (M5 economy).

All three funnel through EconomyService — the economy rule gate (R3/R4/R7/
R8/R10/R11/R12). They never touch SQL/ORM/WS directly and return the same
structured JSON the decision service records in ``llm_runs.tool_result``:
``{"success", "reason", "event"}``.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext

MIN_QUANTITY = 1
MAX_QUANTITY = 99


def _clamp_quantity(quantity: int | None) -> int:
    if quantity is None:
        return 1
    return max(MIN_QUANTITY, min(int(quantity), MAX_QUANTITY))


def _result_json(ok: bool, envelope, reason: str | None) -> str:
    return json.dumps(
        {
            "success": ok,
            "reason": reason,
            "event": envelope.model_dump() if envelope is not None else None,
        },
        ensure_ascii=False,
    )


@function_tool
async def work(
    ctx: RunContextWrapper[AgentToolContext],
    job_id: str,
    reason: str,
) -> str:
    """在当前地点开始一份工作（job_id 必须是可见工作之一）。工作期间不能做其他事，
    完成后一次性结算工资与产物。饱食度=0 或精力=0 时无法工作。"""
    service = ctx.context.engine.economy_service
    if service is None:
        return json.dumps({"success": False, "reason": "经济服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.work_start(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        job_id=job_id,
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def buy_item(
    ctx: RunContextWrapper[AgentToolContext],
    item_id: str,
    reason: str,
    quantity: int = 1,
) -> str:
    """在商店购买商品（item_id 必须是可见商品之一）。钱不够会被拒绝（不能赊账）。"""
    service = ctx.context.engine.economy_service
    if service is None:
        return json.dumps({"success": False, "reason": "经济服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.buy(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        item_id=item_id,
        quantity=_clamp_quantity(quantity),
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def sell_item(
    ctx: RunContextWrapper[AgentToolContext],
    item_id: str,
    reason: str,
    quantity: int = 1,
) -> str:
    """把背包里的物品卖给商店换钱（商店只收购它收购的品类且有库存空间）。"""
    service = ctx.context.engine.economy_service
    if service is None:
        return json.dumps({"success": False, "reason": "经济服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.sell(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        item_id=item_id,
        quantity=_clamp_quantity(quantity),
        reason=reason,
    )
    return _result_json(ok, envelope, err)
