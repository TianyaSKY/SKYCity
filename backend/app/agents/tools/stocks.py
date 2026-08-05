"""buy_stock / sell_stock tools for LLM agents (M10 stock market).

Both funnel through StockService — the stock rule gate (R18.1: idle-only,
no credit, no price impact). They never touch SQL/ORM/WS directly and return
the same structured JSON the decision service records in ``llm_runs.tool_result``:
``{"success", "reason", "event"}``.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext

MAX_SHARES = 9999


def _clamp_shares(shares: int | None) -> int:
    if shares is None:
        return 1
    return max(1, min(int(shares), MAX_SHARES))


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
    """买入小镇公司股票（stock_id 必须是【股票行情】中的 id，股数 1~9999）。钱不够会被拒绝。"""
    service = ctx.context.engine.stock_service
    if service is None:
        return json.dumps({"success": False, "reason": "股票服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.buy_stock(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        stock_id=stock_id,
        shares=_clamp_shares(shares),
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
    """卖出持股变现（不能卖出超过持有的数量）。"""
    service = ctx.context.engine.stock_service
    if service is None:
        return json.dumps({"success": False, "reason": "股票服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.sell_stock(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        stock_id=stock_id,
        shares=_clamp_shares(shares),
        reason=reason,
    )
    return _result_json(ok, envelope, err)
