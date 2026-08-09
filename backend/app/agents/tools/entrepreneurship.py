"""Cooperative-stall tools (R39–R43).

All tools funnel through ShopService — the cooperative-stall rule gate. They
never touch SQL/ORM/WS directly and return the same structured JSON the
decision service records in ``llm_runs.tool_result``:
``{"success", "reason", "event"}``.
"""
from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool
from pydantic import BaseModel

from app.agents.context import AgentToolContext




class ShopProduct(BaseModel):
    """One product line: item id + the seller's price in coins.

    ``buy_price`` (M19, optional) is what the owner pays residents selling
    this item to the store; 0 = the store does not buy (default).
    """

    item_id: str
    price: int
    buy_price: int | None = None


def _result_json(ok: bool, envelope, reason: str | None) -> str:
    return json.dumps(
        {
            "success": ok,
            "reason": reason,
            "event": envelope.model_dump() if envelope is not None else None,
        },
        ensure_ascii=False,
    )


def _as_dict(value) -> dict:
    return value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else dict(value)


@function_tool
async def open_shop(
        ctx: RunContextWrapper[AgentToolContext],
        stall_id: str,
        products: list[ShopProduct],
        reason: str,
) -> str:
    """在自己所在的空合作社摊位开摊。

    ``stall_id`` 必须来自【可开店位置】；开摊会收取 60 金币使用费。商品从
    背包上架，最多 3 种；售价不得低于村庄杂货店同款且不得高于 2 倍基准价；
    可选 ``buy_price`` 不得高于杂货店同款收购价，0 表示不收购。
    """
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps(
            {"success": False, "reason": "合作社摊位服务未初始化", "event": None},
            ensure_ascii=False,
        )
    ok, envelope, err = service.open_shop(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        stall_id=stall_id,
        products=[_as_dict(product) for product in products],
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def stock_shop(
        ctx: RunContextWrapper[AgentToolContext],
        store_id: str,
        item_id: str,
        quantity: int = 1,
        reason: str = "",
) -> str:
    """给自己合作社摊的货架上架背包里的商品（不超过货架容量）。"""
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps({"success": False, "reason": "合作社摊位服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.stock_shop(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        store_id=store_id,
        item_id=item_id,
        quantity=quantity,
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def adjust_price(
        ctx: RunContextWrapper[AgentToolContext],
        store_id: str,
        item_id: str,
        new_price: int,
        reason: str,
) -> str:
    """调整自己合作社摊里某商品的售价（须不低于杂货店同款、不超过 2 倍基准价）。"""
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps({"success": False, "reason": "合作社摊位服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.adjust_price(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        store_id=store_id,
        item_id=item_id,
        new_price=new_price,
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def set_buy_price(
        ctx: RunContextWrapper[AgentToolContext],
        store_id: str,
        item_id: str,
        new_price: int,
        reason: str,
) -> str:
    """设置自己合作社摊某商品的收购价（不高于杂货店同款收购价；0=不收购）。居民可把背包里的
    该商品卖给你的摊位，货款从你余额支付。"""
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps({"success": False, "reason": "合作社摊位服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.set_buy_price(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        store_id=store_id,
        item_id=item_id,
        new_price=new_price,
        reason=reason,
    )
    return _result_json(ok, envelope, err)


@function_tool
async def close_shop(
        ctx: RunContextWrapper[AgentToolContext],
        store_id: str,
        reason: str,
) -> str:
    """收掉自己的合作社摊：货架上的货物退回背包。"""
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps({"success": False, "reason": "合作社摊位服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.close_shop(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        store_id=store_id,
        reason=reason,
    )
    return _result_json(ok, envelope, err)
