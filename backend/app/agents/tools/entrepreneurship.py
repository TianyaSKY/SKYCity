"""open_shop / stock_shop / adjust_price / close_shop tools (M18 R39–R43).

All four funnel through ShopService — the personal-store rule gate. They
never touch SQL/ORM/WS directly and return the same structured JSON the
decision service records in ``llm_runs.tool_result``:
``{"success", "reason", "event"}``.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool
from pydantic import BaseModel

from app.agents.context import AgentToolContext


class ShopLocation(BaseModel):
    """open_shop's site: a map stall id, or a wild-cell (col, row) pair."""

    stall_id: str | None = None
    col: int | None = None
    row: int | None = None


class ShopProduct(BaseModel):
    """One product line: item id + the seller's price in coins."""

    item_id: str
    price: int


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
        location: ShopLocation,
        products: list[ShopProduct],
        reason: str,
) -> str:
    """在空摊位（location 传 {"stall_id": "..."}）或附近可达空地（{"col": N, "row": N}）开店；
    需要至少 150 金币且背包持有商品；products 传 [{"item_id": "...", "price": N}]，
    最多 3 种，价格在 1~2 倍基准价之间。"""
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps({"success": False, "reason": "店铺服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.open_shop(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        location=_as_dict(location),
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
    """给自己店铺的货架上架背包里的商品（不超过货架容量）。"""
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps({"success": False, "reason": "店铺服务未初始化", "event": None}, ensure_ascii=False)
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
    """调整自己店铺里某商品的售价（1~2 倍基准价）。"""
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps({"success": False, "reason": "店铺服务未初始化", "event": None}, ensure_ascii=False)
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
async def close_shop(
        ctx: RunContextWrapper[AgentToolContext],
        store_id: str,
        reason: str,
) -> str:
    """收掉自己的店铺：货架上的货物退回背包。"""
    service = ctx.context.engine.shop_service
    if service is None:
        return json.dumps({"success": False, "reason": "店铺服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.close_shop(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        store_id=store_id,
        reason=reason,
    )
    return _result_json(ok, envelope, err)
