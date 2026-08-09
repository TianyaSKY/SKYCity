"""Stable buy_stock / sell_stock tools for cooperative share actions.

Both funnel through StockService — the cooperative-share rule gate. They
preserve the tool names for callers while presenting subscriptions and
redemptions at a fixed unit price to agents.
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


@function_tool
async def buy_stock(
        ctx: RunContextWrapper[AgentToolContext],
        stock_id: str,
        reason: str,
        shares: int = 1,
) -> str:
    """认购合作社份额（stock_id 来自【合作社份额】；每种最多持有 20 份）。"""
    service = ctx.context.engine.stock_service
    if service is None:
        return json.dumps(
            {"success": False, "reason": "合作社份额服务未初始化", "event": None},
            ensure_ascii=False,
        )
    ok, envelope, err = service.buy_stock(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        stock_id=stock_id,
        shares=shares,
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def sell_stock(
        ctx: RunContextWrapper[AgentToolContext],
        stock_id: str,
        reason: str,
        shares: int = 1,
) -> str:
    """退出持有的合作社份额（不能超过持有数量）。"""
    service = ctx.context.engine.stock_service
    if service is None:
        return json.dumps(
            {"success": False, "reason": "合作社份额服务未初始化", "event": None},
            ensure_ascii=False,
        )
    ok, envelope, err = service.sell_stock(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        stock_id=stock_id,
        shares=shares,
        reason=reason,
    )
    return _result_json(ok, envelope, err)
