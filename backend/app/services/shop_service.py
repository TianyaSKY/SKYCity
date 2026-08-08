"""ShopService: the personal-store rule gate (M18, R39–R43).

Residents open / stock / reprice / close their own shops through the LLM
tools open_shop / stock_shop / adjust_price / close_shop. Every method runs
inside a retrying BEGIN IMMEDIATE transaction (same pattern as EconomyService)
and returns ``(ok, envelope, reason)``; a personal store's sale proceeds go
straight to the owner's balance (EconomyService.buy settlement branch).
"""

from __future__ import annotations

import uuid
from collections import deque
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config.gameplay import (
    OPEN_SHOP_CAPITAL,
    PRICE_MAX_MULT,
    STALL_BUY_MAX_MULT,
    STALL_CAPACITY,
    STALL_CLOSE_HOUR,
    STALL_INITIAL_STOCK,
    STALL_MAX_DISTANCE,
    STALL_MAX_PRODUCTS,
    STALL_OPEN_HOUR,
    STALL_STOCK_CAP,
    STORE_STOCK_INITIAL_PRICE,
)
from app.database.models.agents import Agent
from app.database.models.crops import Crop
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.locations import WorldLocation
from app.database.models.stocks import Stock, StockHolding
from app.database.models.stores import Store, StoreProduct
from app.database.models.structures import TileStructure
from app.database.models.worlds import World
from app.database.unit_of_work import UnitOfWork
from app.services.economy_service import MSG_ITEM_MISSING, MSG_STORE_FULL
from app.world_engine.engine import WorldEngine

# Rejection reasons (Chinese, surfaced in tool results / HTTP 409).
MSG_STALL_OCCUPIED = "该地点已有店铺"
MSG_NOT_AT_STALL = "不在摊位地点"
MSG_TOO_FAR = "距离目标格太远"
MSG_CELL_NOT_AVAILABLE = "目标格不可行走或已被占用"
MSG_UNREACHABLE = "会堵住村庄"  # R39.3 复用文案
MSG_CAPITAL_TOO_LOW = "开店需要至少 100 金币"
MSG_PRODUCT_LIMIT = "最多上架 3 种商品"
MSG_DUPLICATE_PRODUCT = "商品重复"
MSG_PRICE_OUT_OF_RANGE = "价格须不低于村庄杂货店同款售价且不超过 2 倍基准价"
MSG_BUY_PRICE_OUT_OF_RANGE = "收购价须不高于村庄杂货店同款收购价且不超过 1 倍基准价"

# R42: personal-shop prices anchor to the seeded general store — a stall may
# never undercut its selling price nor outbid its buying price, so the two
# store types compete on assortment and location, not on price.
SEED_STORE_ID = "village_shop"
MSG_NOT_OWNER = "不是你的店铺"
MSG_STORE_NOT_FOUND = "店铺不存在"
MSG_PRODUCT_NOT_IN_STORE = "店铺没有该商品"
MSG_NO_ITEM = "背包中没有该物品"
MSG_WORLD_MISSING = "世界不存在"
MSG_PAUSED = "世界已暂停"
MSG_AGENT_MISSING = "智能体不存在"
MSG_BUSY = "当前行动未完成"


class ShopService:
    """Owns the personal-store rule gate for all worlds (one instance, like
    the EconomyService)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory
        self._uow = UnitOfWork(session_factory)

    # ------------------------------------------------------------------ #
    # Open shop (R39)
    # ------------------------------------------------------------------ #

    def open_shop(
            self,
            world_id: str,
            agent_id: str,
            location: dict | None,
            products: list[dict] | None,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Open a personal store at a map stall or a nearby wild cell.

        ``location`` = ``{"stall_id": "..."}`` (must be the agent's current
        location) or ``{"col": N, "row": N}`` (within STALL_MAX_DISTANCE,
        walkable, unreserved and reachable). ``products`` =
        ``[{"item_id": str, "price": int, "buy_price"?: int}]``, at most
        STALL_MAX_PRODUCTS entries priced within the R42 bounds (anchored to
        the seeded village store's sell/buy prices).
        """

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                return False, None, MSG_BUSY  # R1: one action at a time
            loc_arg = location or {}
            product_entries = products or []

            # --- site selection (R39.2) ---
            stall_id = loc_arg.get("stall_id")
            if stall_id is not None:
                stall = session.get(
                    WorldLocation, {"world_id": world_id, "location_id": stall_id}
                )
                if stall is None or agent.location_id != stall_id:
                    return False, None, MSG_NOT_AT_STALL
                col, row = stall.col, stall.row
                target_location_id = stall_id
            else:
                try:
                    col = int(loc_arg["col"])
                    row = int(loc_arg["row"])
                except (KeyError, TypeError, ValueError):
                    return False, None, MSG_CELL_NOT_AVAILABLE
                if abs(agent.col - col) + abs(agent.row - row) > STALL_MAX_DISTANCE:
                    return False, None, MSG_TOO_FAR
                if not self._cell_available(session, world_id, col, row):
                    return False, None, MSG_CELL_NOT_AVAILABLE
                if not self._reachable(session, world_id, col, row):
                    return False, None, MSG_UNREACHABLE
                target_location_id = f"stall_{uuid.uuid4().hex}"

            # --- one store per location (R39.4, unique index as backstop) ---
            if (
                session.scalar(
                    select(Store).where(
                        Store.world_id == world_id,
                        Store.location_id == target_location_id,
                    )
                )
                is not None
            ):
                return False, None, MSG_STALL_OCCUPIED

            # --- capital threshold (R39.5: gate only, no deduction) ---
            if agent.money < OPEN_SHOP_CAPITAL:
                return False, None, MSG_CAPITAL_TOO_LOW

            # --- product lines (R39.6 / R42; M19: optional buy_price) ---
            if not 1 <= len(product_entries) <= STALL_MAX_PRODUCTS:
                return False, None, MSG_PRODUCT_LIMIT
            seen: set[str] = set()
            resolved: list[tuple[Item, int, int, Inventory]] = []
            for entry in product_entries:
                item_id = str(entry.get("item_id") or "")
                if item_id in seen:
                    return False, None, MSG_DUPLICATE_PRODUCT
                seen.add(item_id)
                try:
                    price = int(entry["price"])
                except (KeyError, TypeError, ValueError):
                    return False, None, MSG_PRICE_OUT_OF_RANGE
                try:
                    buy_price = int(entry.get("buy_price") or 0)
                except (TypeError, ValueError):
                    return False, None, MSG_BUY_PRICE_OUT_OF_RANGE
                item = session.get(Item, {"world_id": world_id, "item_id": item_id})
                if item is None:
                    return False, None, MSG_ITEM_MISSING
                floor, cap, buy_cap = self._price_bounds(
                    session, world_id, item_id, item.base_price
                )
                if not floor <= price <= cap:
                    return False, None, MSG_PRICE_OUT_OF_RANGE
                if not 0 <= buy_price <= buy_cap:
                    return False, None, MSG_BUY_PRICE_OUT_OF_RANGE
                inventory = session.get(
                    Inventory,
                    {"world_id": world_id, "agent_id": agent_id, "item_id": item_id},
                )
                if inventory is None or inventory.quantity < 1:
                    return False, None, MSG_NO_ITEM
                resolved.append((item, price, buy_price, inventory))

            # --- write path (same transaction) ---
            first_item = resolved[0][0]
            store_name = f"{agent.name}的{first_item.name}摊"
            if stall_id is None:
                session.add(
                    WorldLocation(
                        world_id=world_id,
                        location_id=target_location_id,
                        name=store_name,
                        location_type="stall",
                        col=col,
                        row=row,
                        capacity=STALL_CAPACITY,
                        open_hour=STALL_OPEN_HOUR,
                        close_hour=STALL_CLOSE_HOUR,
                    )
                )
            store_id = f"store_{uuid.uuid4().hex}"
            session.add(
                Store(
                    world_id=world_id,
                    store_id=store_id,
                    location_id=target_location_id,
                    company_id=None,
                    owner_agent_id=agent_id,
                    name=store_name,
                )
            )
            products_payload: list[dict[str, Any]] = []
            for item, price, buy_price, inventory in resolved:
                stock = min(inventory.quantity, STALL_INITIAL_STOCK)
                session.add(
                    StoreProduct(
                        world_id=world_id,
                        store_id=store_id,
                        item_id=item.item_id,
                        sell_price=price,
                        base_sell_price=price,
                        buy_price=buy_price,
                        stock=stock,
                        stock_cap=STALL_STOCK_CAP,
                        restock_daily=0,
                    )
                )
                inventory.quantity -= stock
                if inventory.quantity <= 0:
                    session.delete(inventory)
                products_payload.append(
                    {
                        "item_id": item.item_id,
                        "sell_price": price,
                        "buy_price": buy_price,
                        "stock": stock,
                    }
                )
            # R18.2: the personal store lists itself on the market; the row
            # lives and dies with the shop (close_shop deletes it).
            session.add(
                Stock(
                    world_id=world_id,
                    stock_id=f"stock_{store_id}",
                    name=f"{store_name}股票",
                    company_id=store_id,
                    source="store",
                    base_price=STORE_STOCK_INITIAL_PRICE,
                    price=STORE_STOCK_INITIAL_PRICE,
                    prev_price=STORE_STOCK_INITIAL_PRICE,
                    outstanding_shares=100,
                )
            )
            location_row = session.get(
                WorldLocation,
                {"world_id": world_id, "location_id": target_location_id},
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "store_opened",
                {
                    "store_id": store_id,
                    "name": store_name,
                    "owner_agent_id": agent_id,
                    "location_id": target_location_id,
                    "col": location_row.col if location_row is not None else col,
                    "row": location_row.row if location_row is not None else row,
                    "products": products_payload,
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Stock the shelf (R41)
    # ------------------------------------------------------------------ #

    def stock_shop(
            self,
            world_id: str,
            agent_id: str,
            store_id: str,
            item_id: str,
            quantity: int = 1,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Move backpack items onto the owner's own shelf (cap-guarded)."""

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                return False, None, MSG_BUSY
            store = session.get(Store, {"world_id": world_id, "store_id": store_id})
            if store is None:
                return False, None, MSG_STORE_NOT_FOUND
            if store.owner_agent_id != agent_id:
                return False, None, MSG_NOT_OWNER
            product = session.get(
                StoreProduct,
                {"world_id": world_id, "store_id": store_id, "item_id": item_id},
            )
            if product is None:
                return False, None, MSG_PRODUCT_NOT_IN_STORE
            qty = max(1, min(int(quantity), 99))
            inventory = session.get(
                Inventory,
                {"world_id": world_id, "agent_id": agent_id, "item_id": item_id},
            )
            if inventory is None or inventory.quantity < qty:
                return False, None, MSG_NO_ITEM
            result = session.execute(
                update(StoreProduct)
                .where(
                    StoreProduct.world_id == world_id,
                    StoreProduct.store_id == store_id,
                    StoreProduct.item_id == item_id,
                    StoreProduct.stock + qty <= StoreProduct.stock_cap,
                )
                .values(stock=StoreProduct.stock + qty)
            )
            if result.rowcount == 0:
                return False, None, MSG_STORE_FULL
            inventory.quantity -= qty
            if inventory.quantity <= 0:
                session.delete(inventory)
            product = session.get(
                StoreProduct,
                {"world_id": world_id, "store_id": store_id, "item_id": item_id},
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "store_stocked",
                {
                    "store_id": store_id,
                    "owner_agent_id": agent_id,
                    "item_id": item_id,
                    "quantity": qty,
                    "stock_after": product.stock if product is not None else qty,
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Adjust price (R42)
    # ------------------------------------------------------------------ #

    def adjust_price(
            self,
            world_id: str,
            agent_id: str,
            store_id: str,
            item_id: str,
            new_price: int,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Reprice one product of the owner's own store (R42 bounds)."""

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                return False, None, MSG_BUSY
            store = session.get(Store, {"world_id": world_id, "store_id": store_id})
            if store is None:
                return False, None, MSG_STORE_NOT_FOUND
            if store.owner_agent_id != agent_id:
                return False, None, MSG_NOT_OWNER
            product = session.get(
                StoreProduct,
                {"world_id": world_id, "store_id": store_id, "item_id": item_id},
            )
            if product is None:
                return False, None, MSG_PRODUCT_NOT_IN_STORE
            try:
                price = int(new_price)
            except (TypeError, ValueError):
                return False, None, MSG_PRICE_OUT_OF_RANGE
            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            base = item.base_price if item is not None else product.base_sell_price
            floor, cap, _ = self._price_bounds(session, world_id, item_id, base)
            if not floor <= price <= cap:
                return False, None, MSG_PRICE_OUT_OF_RANGE
            product.sell_price = price
            product.base_sell_price = price
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "store_price_changed",
                {
                    "store_id": store_id,
                    "item_id": item_id,
                    "item_name": item.name if item is not None else item_id,
                    "sell_price": price,
                    "promo": False,
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Set buy price (M19: personal stores may buy from residents)
    # ------------------------------------------------------------------ #

    def set_buy_price(
            self,
            world_id: str,
            agent_id: str,
            store_id: str,
            item_id: str,
            new_price: int,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Set the owner's own store's purchase price for one item.

        Valid range is 0..buy_cap (R42: never above the village store's buy
        price for the item; 0 = the store does not buy). Buyers sell through
        EconomyService.sell, which settles from the owner's balance (R7: no
        credit).
        """

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                return False, None, MSG_BUSY
            store = session.get(Store, {"world_id": world_id, "store_id": store_id})
            if store is None:
                return False, None, MSG_STORE_NOT_FOUND
            if store.owner_agent_id != agent_id:
                return False, None, MSG_NOT_OWNER
            product = session.get(
                StoreProduct,
                {"world_id": world_id, "store_id": store_id, "item_id": item_id},
            )
            if product is None:
                return False, None, MSG_PRODUCT_NOT_IN_STORE
            try:
                price = int(new_price)
            except (TypeError, ValueError):
                return False, None, MSG_BUY_PRICE_OUT_OF_RANGE
            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            base = item.base_price if item is not None else product.base_sell_price
            _, _, buy_cap = self._price_bounds(session, world_id, item_id, base)
            if not 0 <= price <= buy_cap:
                return False, None, MSG_BUY_PRICE_OUT_OF_RANGE
            product.buy_price = price
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "store_buy_price_changed",
                {
                    "store_id": store_id,
                    "item_id": item_id,
                    "item_name": item.name if item is not None else item_id,
                    "buy_price": price,
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Close shop (R43)
    # ------------------------------------------------------------------ #

    def close_shop(
            self,
            world_id: str,
            agent_id: str,
            store_id: str,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Close the owner's own store: shelf stock returns to the backpack,
        the store (and a wild-cell stall location) is deleted, and the
        listing is taken off the market."""

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                return False, None, MSG_BUSY
            store = session.get(Store, {"world_id": world_id, "store_id": store_id})
            if store is None:
                return False, None, MSG_STORE_NOT_FOUND
            if store.owner_agent_id != agent_id:
                return False, None, MSG_NOT_OWNER

            products = session.scalars(
                select(StoreProduct).where(
                    StoreProduct.world_id == world_id,
                    StoreProduct.store_id == store_id,
                )
            ).all()
            for product in products:
                if product.stock > 0:
                    self._add_inventory(
                        session, world_id, agent_id, product.item_id, product.stock
                    )
                session.delete(product)
            # R43: 歇业即退市 — the listing and any holdings disappear
            # (explicit deletes: SQLite sessions run with FKs off, so the
            # ondelete=CASCADE on stock_holdings/stores would not fire).
            stock = session.scalars(
                select(Stock).where(
                    Stock.world_id == world_id,
                    Stock.company_id == store_id,
                    Stock.source == "store",
                )
            ).all()
            for row in stock:
                for holding in session.scalars(
                        select(StockHolding).where(
                            StockHolding.world_id == world_id,
                            StockHolding.stock_id == row.stock_id,
                        )
                ).all():
                    session.delete(holding)
                session.delete(row)
            # A wild-cell stall location dies with its store; map stalls live on.
            map_location_ids = {
                loc.location_id for loc in self.engine.world_config.locations
            }
            if store.location_id not in map_location_ids:
                location_row = session.get(
                    WorldLocation,
                    {"world_id": world_id, "location_id": store.location_id},
                )
                if location_row is not None:
                    session.delete(location_row)
            session.delete(store)
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "store_closed",
                {
                    "store_id": store_id,
                    "owner_agent_id": agent_id,
                    "reason": reason or "自主收摊",
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _cell_available(self, session: Session, world_id: str, col: int, row: int) -> bool:
        """R39.2: walkable, unreserved, not a location anchor / spawn cell."""
        if (col, row) not in self.engine.effective_walkable(session, world_id):
            return False
        if session.get(
                TileStructure, {"world_id": world_id, "col": col, "row": row}
        ) is not None:
            return False
        if session.get(Crop, {"world_id": world_id, "col": col, "row": row}) is not None:
            return False
        reserved = {
            (loc.col, loc.row)
            for loc in session.scalars(
                select(WorldLocation).where(WorldLocation.world_id == world_id)
            )
        } | {(sp.col, sp.row) for sp in self.engine.world_config.spawn_points}
        return (col, row) not in reserved

    def _reachable(self, session: Session, world_id: str, col: int, row: int) -> bool:
        """R39.3: the target shares the main walkable component with the
        spawn network (BFS from the first spawn point, 4-dir)."""
        spawns = list(self.engine.world_config.spawn_points)
        if not spawns:
            return False
        start = (spawns[0].col, spawns[0].row)
        walkable = self.engine.effective_walkable(session, world_id)
        if start not in walkable:
            return False
        seen = {start}
        frontier: deque[tuple[int, int]] = deque([start])
        while frontier:
            c, r = frontier.popleft()
            for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbour = (c + dc, r + dr)
                if neighbour in seen or neighbour not in walkable:
                    continue
                seen.add(neighbour)
                frontier.append(neighbour)
        return (col, row) in seen

    def _price_bounds(
            self,
            session: Session,
            world_id: str,
            item_id: str,
            base_price: int,
    ) -> tuple[int, int, int]:
        """Personal-shop price bounds anchored to the seeded village store (R42).

        Selling floor = the village store's base sell price for the same item,
        so a stall can never undercut the general store; buying cap = the
        village store's buy price (when it buys), so a stall can never outbid
        it. Items the village store does not carry keep the plain base-price
        bounds. Returns ``(sell_floor, sell_cap, buy_cap)``.
        """
        floor, cap = 1, round(base_price * PRICE_MAX_MULT)
        buy_cap = round(base_price * STALL_BUY_MAX_MULT)
        village = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": SEED_STORE_ID, "item_id": item_id},
        )
        if village is not None:
            floor = max(floor, village.base_sell_price)
            cap = max(cap, floor)
            if village.buy_price > 0:
                buy_cap = min(buy_cap, village.buy_price)
        return floor, cap, buy_cap

    def _add_inventory(
            self, session: Session, world_id: str, agent_id: str, item_id: str, quantity: int
    ) -> None:
        row = session.get(
            Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
        )
        if row is None:
            session.add(
                Inventory(
                    world_id=world_id,
                    agent_id=agent_id,
                    item_id=item_id,
                    quantity=quantity,
                )
            )
        else:
            row.quantity += quantity
