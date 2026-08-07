"""transfer_money / give_item tools for LLM agents (M11 agent-to-agent).

Both funnel through TransferService — the transfer/gift rule gate (R19.1:
initiator idle, target within 3 cells; R19.2: no credit / no over-giving).
They never touch SQL/ORM/WS directly and return the same structured JSON the
decision service records in ``llm_runs.tool_result``:
``{"success", "reason", "event"}``.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext

MAX_TRANSFER_AMOUNT = 1_000_000
MAX_GIFT_QUANTITY = 99


def _clamp_amount(amount: int | None) -> int:
    if amount is None:
        return 1
    return max(1, min(int(amount), MAX_TRANSFER_AMOUNT))


def _clamp_quantity(quantity: int | None) -> int:
    if quantity is None:
        return 1
    return max(1, min(int(quantity), MAX_GIFT_QUANTITY))


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
async def transfer_money(
        ctx: RunContextWrapper[AgentToolContext],
        target_agent_id: str,
        amount: int,
        reason: str,
) -> str:
    """给附近的智能体转账金币（target_agent_id 必须是【可见人物】里的 id，距离 ≤ 3 格）。钱不够会被拒绝。"""
    service = ctx.context.engine.transfer_service
    if service is None:
        return json.dumps({"success": False, "reason": "转账服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.transfer_money(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        to_agent_id=target_agent_id,
        amount=_clamp_amount(amount),
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def give_item(
        ctx: RunContextWrapper[AgentToolContext],
        target_agent_id: str,
        item_id: str,
        reason: str,
        quantity: int = 1,
) -> str:
    """把背包里的物品送给附近的智能体（不能超过持有数量）。"""
    service = ctx.context.engine.transfer_service
    if service is None:
        return json.dumps({"success": False, "reason": "转账服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.give_item(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        to_agent_id=target_agent_id,
        item_id=item_id,
        quantity=_clamp_quantity(quantity),
        reason=reason,
    )
    return _result_json(ok, envelope, err)
