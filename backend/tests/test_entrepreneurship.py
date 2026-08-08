"""M18 tests: personal shops (R39–R43) — open/stock/reprice/close through
ShopService, the owner-settlement branch in EconomyService.buy, the stock
credit path (R18.2), the R15/R40 exclusion, and the full autonomous chain.

Drives the WorldEngine directly (no HTTP, no background loop): clock advanced
via tick + engine._tick_runtime, exactly like test_economy.py.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.gameplay import (
    OPEN_SHOP_CAPITAL,
    PRICE_MAX_MULT,
    STALL_CAPACITY,
    STALL_CLOSE_HOUR,
    STALL_INITIAL_STOCK,
    STALL_MAX_DISTANCE,
    STALL_OPEN_HOUR,
    STALL_STOCK_CAP,
    STORE_STOCK_INITIAL_PRICE,
)
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.llm_runs import LLMRun
from app.database.models.locations import WorldLocation
from app.database.models.stocks import Stock
from app.database.models.stores import Store, StoreProduct
from app.database.models.structures import TileStructure
from app.database.models.transactions import Transaction
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.economy_service import (
    MSG_NOT_AT_STORE,
    MSG_PRODUCT_MISSING,
    MSG_STORE_FULL,
    EconomyService,
)
from app.services.shop_service import (
    MSG_BUY_PRICE_OUT_OF_RANGE,
    MSG_CAPITAL_TOO_LOW,
    MSG_DUPLICATE_PRODUCT,
    MSG_NOT_OWNER,
    MSG_NOT_AT_STALL,
    MSG_PRICE_OUT_OF_RANGE,
    MSG_PRODUCT_LIMIT,
    MSG_STALL_OCCUPIED,
    MSG_STORE_NOT_FOUND,
    MSG_TOO_FAR,
    MSG_UNREACHABLE,
    ShopService,
)
from app.services.stock_service import StockService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_world_engine import advance_minutes, agent_row

STALL_1 = "stall_plaza_1"
STALL_2 = "stall_plaza_2"
STALL_1_ANCHOR = (30, 19)
STALL_2_ANCHOR = (33, 19)
VILLAGE_SHOP_STOCK = "stock_village_shop"


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


def make_engine(
        world_config: ParsedWorldConfig,
        scripts=None,
        wire_decisions: bool = False,
        provider=None,
) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.economy_service = EconomyService(eng, SessionLocal)
    eng.stock_service = StockService(eng, SessionLocal)
    eng.shop_service = ShopService(eng, SessionLocal)
    if wire_decisions:
        eng.decision_service = DecisionService(
            eng,
            SessionLocal,
            provider=provider or FakeDecisionProvider(scripts=scripts),
        )
    return eng


@pytest.fixture()
def engine(world_config: ParsedWorldConfig) -> WorldEngine:
    eng = make_engine(world_config)
    yield eng
    eng._runtimes.clear()


def place_agent(
        engine: WorldEngine, world_id: str, agent_id: str, location_id: str, col: int, row: int
) -> None:
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert agent is not None
        agent.location_id = location_id
        agent.col = col
        agent.row = row
        session.commit()
    finally:
        session.close()


def set_agent(
        engine: WorldEngine, world_id: str, agent_id: str, **fields
) -> None:
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert agent is not None
        for key, value in fields.items():
            setattr(agent, key, value)
        session.commit()
    finally:
        session.close()


def add_inventory(
        engine: WorldEngine, world_id: str, agent_id: str, item_id: str, quantity: int
) -> None:
    session = SessionLocal()
    try:
        existing = session.get(
            Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
        )
        if existing is None:
            session.add(
                Inventory(
                    world_id=world_id, agent_id=agent_id, item_id=item_id, quantity=quantity
                )
            )
        else:
            existing.quantity += quantity
        session.commit()
    finally:
        session.close()


def inventory_of(engine: WorldEngine, world_id: str, agent_id: str) -> dict[str, int]:
    session = SessionLocal()
    try:
        rows = session.scalars(
            select(Inventory).where(
                Inventory.world_id == world_id, Inventory.agent_id == agent_id
            )
        ).all()
        return {row.item_id: row.quantity for row in rows}
    finally:
        session.close()


def transaction_rows(engine: WorldEngine, world_id: str, agent_id: str):
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(Transaction).where(
                    Transaction.world_id == world_id, Transaction.agent_id == agent_id
                )
            ).all()
        )
    finally:
        session.close()


def store_rows(engine: WorldEngine, world_id: str) -> list[Store]:
    """Personal stores only (the seeded village_shop is not a personal shop)."""
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(Store)
                .where(
                    Store.world_id == world_id,
                    Store.owner_agent_id.is_not(None),
                )
                .order_by(Store.store_id)
            ).all()
        )
    finally:
        session.close()


def store_of(engine: WorldEngine, world_id: str, store_id: str) -> Store | None:
    session = SessionLocal()
    try:
        return session.get(Store, {"world_id": world_id, "store_id": store_id})
    finally:
        session.close()


def stock_row(engine: WorldEngine, world_id: str, company_id: str) -> Stock | None:
    session = SessionLocal()
    try:
        return session.scalars(
            select(Stock).where(
                Stock.world_id == world_id,
                Stock.source == "store",
                Stock.company_id == company_id,
            )
        ).first()
    finally:
        session.close()


def _open_stall(
        engine: WorldEngine,
        world_id: str,
        agent_id: str = "agent_linxia",
        stall_id: str = STALL_1,
        item_id: str = "wheat",
        price: int = 6,  # R42: 小麦个人店售价下限=杂货店同款 6
        stock_qty: int = 10,
) -> tuple[bool, object, str | None]:
    place_agent(engine, world_id, agent_id, stall_id, *STALL_1_ANCHOR)
    set_agent(engine, world_id, agent_id, money=200)
    add_inventory(engine, world_id, agent_id, item_id, stock_qty)
    return engine.shop_service.open_shop(
        world_id,
        agent_id,
        {"stall_id": stall_id},
        [{"item_id": item_id, "price": price}],
        reason="摆摊",
    )


def _add_structure(
        engine: WorldEngine, world_id: str, col: int, row: int, agent_id: str
) -> None:
    session = SessionLocal()
    try:
        session.add(
            TileStructure(
                world_id=world_id,
                col=col,
                row=row,
                blueprint_id="fence_wood",
                owner_agent_id=agent_id,
                status="built",
                built_at=480,
                materials_json={},
            )
        )
        session.commit()
    finally:
        session.close()


def _remove_structure(engine: WorldEngine, world_id: str, col: int, row: int) -> None:
    session = SessionLocal()
    try:
        row_obj = session.get(
            TileStructure, {"world_id": world_id, "col": col, "row": row}
        )
        if row_obj is not None:
            session.delete(row_obj)
            session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Open shop (R39)
# --------------------------------------------------------------------------- #


def test_open_stall_success(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = _open_stall(engine, world_id)
    assert ok is True and reason is None
    assert envelope.type == "store_opened"
    assert envelope.payload["location_id"] == STALL_1
    assert envelope.payload["col"] == STALL_1_ANCHOR[0]
    assert envelope.payload["row"] == STALL_1_ANCHOR[1]
    assert envelope.payload["products"] == [
        {"item_id": "wheat", "sell_price": 6, "buy_price": 0, "stock": STALL_INITIAL_STOCK}
    ]

    store = store_rows(engine, world_id)[0]
    assert store.owner_agent_id == "agent_linxia"
    assert store.name == "林夏的小麦摊"
    assert store.company_id is None
    # 开店不扣资本门槛
    assert agent_row(engine, world_id, "agent_linxia").money == 200

    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": store.store_id, "item_id": "wheat"},
        )
        assert product.stock == STALL_INITIAL_STOCK  # min(10, 5)
        assert product.sell_price == 6
        assert product.base_sell_price == 6
        assert product.buy_price == 0
        assert product.stock_cap == STALL_STOCK_CAP
        assert product.restock_daily == 0
        # R18.2: the personal shop lists itself on the market.
        listing = session.scalars(
            select(Stock).where(
                Stock.world_id == world_id,
                Stock.source == "store",
                Stock.company_id == store.store_id,
            )
        ).first()
        assert listing is not None
        assert listing.price == STORE_STOCK_INITIAL_PRICE
        assert listing.outstanding_shares == 100
    finally:
        session.close()
    # 首单货从背包扣减
    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 5}


def test_open_shop_capital_too_low(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", money=OPEN_SHOP_CAPITAL - 50)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6}], reason="摆摊",
    )
    assert ok is False and envelope is None
    assert reason == MSG_CAPITAL_TOO_LOW
    assert store_rows(engine, world_id) == []
    assert stock_row(engine, world_id, "whatever") is None
    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 10}


def test_open_shop_stall_occupied(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    assert _open_stall(engine, world_id)[0] is True
    # 第二家在同一个摊位 → 拒绝（须身在摊位地点）
    place_agent(engine, world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, "agent_zhangming", money=200)
    add_inventory(engine, world_id, "agent_zhangming", "wheat", 10)
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id, "agent_zhangming", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6}], reason="抢摊位",
    )
    assert ok is False and envelope is None
    assert reason == MSG_STALL_OCCUPIED
    assert len(store_rows(engine, world_id)) == 1


def test_open_shop_not_at_stall(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    # 人在广场，不在摊位地点 → 拒绝
    place_agent(engine, world_id, "agent_linxia", "village_plaza", 32, 20)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6}], reason="摆摊",
    )
    assert ok is False and reason == MSG_NOT_AT_STALL


def test_open_shop_product_rules(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)

    # 商品种类上限
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [
            {"item_id": "wheat", "price": 6},
            {"item_id": "wood", "price": 10},
            {"item_id": "egg", "price": 6},
            {"item_id": "apple", "price": 6},
        ],
        reason="摆摊",
    )
    assert ok is False and reason == MSG_PRODUCT_LIMIT
    # 重复商品
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6}, {"item_id": "wheat", "price": 6}],
        reason="摆摊",
    )
    assert ok is False and reason == MSG_DUPLICATE_PRODUCT
    # 价格越界（wheat 基准 3，上限 round(3×2)=6）
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 0}], reason="摆摊",
    )
    assert ok is False and reason == MSG_PRICE_OUT_OF_RANGE
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 7}], reason="摆摊",
    )
    assert ok is False and reason == MSG_PRICE_OUT_OF_RANGE
    # 背包没有该物品
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "honey", "price": 20}], reason="摆摊",
    )
    assert ok is False and reason == "背包中没有该物品"
    assert store_rows(engine, world_id) == []


def test_open_shop_price_anchored_to_village_store(engine: WorldEngine) -> None:
    """R42 错位竞争：个人店售价不得低于杂货店同款、收购价不得高于杂货店
    同款——杂货店（小麦售 6 / 收 4）是价格锚点。"""
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)

    # 售价低于杂货店（wheat 杂货店卖 6）→ 拒绝
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 5}], reason="摆摊",
    )
    assert ok is False and reason == MSG_PRICE_OUT_OF_RANGE
    # 售价等于杂货店 → 允许
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6}], reason="摆摊",
    )
    assert ok is True, reason
    # 收购价高于杂货店（wheat 杂货店收 4，个人店上限 min(3, 4)=3）→ 拒绝
    place_agent(engine, world_id, "agent_linxia", STALL_2, *STALL_2_ANCHOR)
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_2},
        [{"item_id": "wheat", "price": 6, "buy_price": 4}], reason="摆摊兼收购",
    )
    assert ok is False and reason == MSG_BUY_PRICE_OUT_OF_RANGE
    # 收购价 ≤ min(1×基准, 杂货店收购价) → 允许
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_2},
        [{"item_id": "wheat", "price": 6, "buy_price": 3}], reason="摆摊兼收购",
    )
    assert ok is True, reason


def test_open_shop_multiple_stores_same_owner(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, _, reason = _open_stall(engine, world_id, stall_id=STALL_1)
    assert ok is True, reason
    place_agent(engine, world_id, "agent_linxia", STALL_2, *STALL_2_ANCHOR)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_2},
        [{"item_id": "wheat", "price": 6}], reason="第二家店",
    )
    assert ok is True, reason
    stores = store_rows(engine, world_id)
    assert len(stores) == 2
    assert {store.owner_agent_id for store in stores} == {"agent_linxia"}
    assert {store.location_id for store in stores} == {STALL_1, STALL_2}


def test_open_shop_wild_cell_success(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", None, 31, 20)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"col": 31, "row": 21},
        [{"item_id": "wheat", "price": 6}], reason="荒地摆摊",
    )
    assert ok is True, reason
    assert envelope.payload["location_id"].startswith("stall_")
    session = SessionLocal()
    try:
        location = session.get(
            WorldLocation,
            {"world_id": world_id, "location_id": envelope.payload["location_id"]},
        )
        assert location is not None
        assert location.location_type == "stall"
        assert (location.col, location.row) == (31, 21)
        assert location.capacity == STALL_CAPACITY
        assert location.open_hour == STALL_OPEN_HOUR
        assert location.close_hour == STALL_CLOSE_HOUR
        assert location.name == "林夏的小麦摊"
    finally:
        session.close()


def test_open_shop_wild_cell_too_far(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", None, 31, 20)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"col": 35, "row": 20},  # 距离 4 > STALL_MAX_DISTANCE
        [{"item_id": "wheat", "price": 6}], reason="荒地摆摊",
    )
    assert ok is False and reason == MSG_TOO_FAR
    # 距离恰好 ≤3 的合法格不受距离限制
    assert STALL_MAX_DISTANCE == 3
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"col": 34, "row": 20},  # 距离 3
        [{"item_id": "wheat", "price": 6}], reason="荒地摆摊",
    )
    assert ok is True, reason


def test_open_shop_wild_cell_reachability(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", None, 31, 20)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)

    # 三面围墙：目标格仍可从 (31,20) 到达 → 开店成功（店不挡路，结构仍在）
    for col, row in ((30, 21), (32, 21), (31, 22)):
        _add_structure(engine, world_id, col, row, "agent_linxia")
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"col": 31, "row": 21},
        [{"item_id": "wheat", "price": 6}], reason="荒地摆摊",
    )
    assert ok is True, reason
    store_id = envelope.payload["store_id"]
    ok, _, reason = engine.shop_service.close_shop(
        world_id, "agent_linxia", store_id, reason="收摊"
    )
    assert ok is True, reason

    # 四面围死 → 不可达
    _add_structure(engine, world_id, 31, 20, "agent_linxia")
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"col": 31, "row": 21},
        [{"item_id": "wheat", "price": 6}], reason="荒地摆摊",
    )
    assert ok is False and reason == MSG_UNREACHABLE
    # 拆掉一面墙 → 可达，重新开店成功
    _remove_structure(engine, world_id, 31, 20)
    ok, _, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"col": 31, "row": 21},
        [{"item_id": "wheat", "price": 6}], reason="荒地摆摊",
    )
    assert ok is True, reason


# --------------------------------------------------------------------------- #
# Stock / reprice / close (R41/R42/R43)
# --------------------------------------------------------------------------- #


def test_stock_shop_success_and_full(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = _open_stall(engine, world_id)
    assert ok is True, reason
    store_id = envelope.payload["store_id"]
    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 5}

    ok, envelope, reason = engine.shop_service.stock_shop(
        world_id, "agent_linxia", store_id, "wheat", quantity=3, reason="补货"
    )
    assert ok is True and reason is None
    assert envelope.type == "store_stocked"
    assert envelope.payload == {
        "store_id": store_id,
        "owner_agent_id": "agent_linxia",
        "item_id": "wheat",
        "quantity": 3,
        "stock_after": STALL_INITIAL_STOCK + 3,
    }
    # 背包只有 2 件，先补齐再填满货架到容量上限
    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 2}
    add_inventory(engine, world_id, "agent_linxia", "wheat", 20)
    for _ in range(6):  # 8 -> 20（6 × 2）
        ok, _, reason = engine.shop_service.stock_shop(
            world_id, "agent_linxia", store_id, "wheat", quantity=2, reason="补货"
        )
        assert ok is True, reason
    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 10}
    # 货架已满 → 拒绝且背包不动
    ok, _, reason = engine.shop_service.stock_shop(
        world_id, "agent_linxia", store_id, "wheat", quantity=2, reason="补货"
    )
    assert ok is False and reason == MSG_STORE_FULL
    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 10}


def test_adjust_price(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = _open_stall(engine, world_id)
    assert ok is True, reason
    store_id = envelope.payload["store_id"]

    ok, envelope, reason = engine.shop_service.adjust_price(
        world_id, "agent_linxia", store_id, "wheat", 6, reason="涨价"
    )
    assert ok is True and reason is None
    assert envelope.type == "store_price_changed"
    assert envelope.payload == {
        "store_id": store_id,
        "item_id": "wheat",
        "item_name": "小麦",
        "sell_price": 6,
        "promo": False,
    }
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": store_id, "item_id": "wheat"},
        )
        assert product.sell_price == 6
        assert product.base_sell_price == 6
    finally:
        session.close()

    for bad in (0, 7, -1):  # wheat 基准 3：合法区间 1~6
        ok, _, reason = engine.shop_service.adjust_price(
            world_id, "agent_linxia", store_id, "wheat", bad, reason="乱调价"
        )
        assert ok is False and reason == MSG_PRICE_OUT_OF_RANGE, bad
    # 上限取整：round(3 × 2.0) = 6
    assert round(3 * PRICE_MAX_MULT) == 6


def test_close_shop(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = _open_stall(engine, world_id)
    assert ok is True, reason
    store_id = envelope.payload["store_id"]
    # 上架 3 件 → 货架 8，背包 2
    ok, _, reason = engine.shop_service.stock_shop(
        world_id, "agent_linxia", store_id, "wheat", quantity=3, reason="补货"
    )
    assert ok is True, reason

    ok, envelope, reason = engine.shop_service.close_shop(
        world_id, "agent_linxia", store_id, reason="收摊"
    )
    assert ok is True and reason is None
    assert envelope.type == "store_closed"
    assert envelope.payload == {
        "store_id": store_id,
        "owner_agent_id": "agent_linxia",
        "reason": "收摊",
    }
    # 货架货物退回背包：开店前 10 = 背包 2 + 货架 8
    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 10}
    session = SessionLocal()
    try:
        assert session.get(Store, {"world_id": world_id, "store_id": store_id}) is None
        assert (
            session.scalars(
                select(StoreProduct).where(
                    StoreProduct.world_id == world_id,
                    StoreProduct.store_id == store_id,
                )
            ).first()
            is None
        )
        assert (
            session.scalars(
                select(Stock).where(
                    Stock.world_id == world_id,
                    Stock.company_id == store_id,
                    Stock.source == "store",
                )
            ).first()
            is None
        )
    finally:
        session.close()
    # 原摊位可再开店
    ok, _, reason = _open_stall(engine, world_id)
    assert ok is True, reason
    # 关店后在同一地点买不到该商品（个人店已不存在）
    ok, _, reason = engine.economy_service.buy(
        world_id, "agent_zhangming", "wheat", quantity=1, reason="买小麦"
    )
    assert ok is False
    assert reason in (MSG_PRODUCT_MISSING, MSG_NOT_AT_STORE)


def test_close_shop_wild_cell_deletes_location(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", None, 31, 20)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"col": 31, "row": 21},
        [{"item_id": "wheat", "price": 6}], reason="荒地摆摊",
    )
    assert ok is True, reason
    location_id = envelope.payload["location_id"]
    ok, _, reason = engine.shop_service.close_shop(
        world_id, "agent_linxia", envelope.payload["store_id"], reason="收摊"
    )
    assert ok is True, reason
    session = SessionLocal()
    try:
        assert (
            session.get(
                WorldLocation, {"world_id": world_id, "location_id": location_id}
            )
            is None
        )
    finally:
        session.close()


def test_shop_owner_only(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = _open_stall(engine, world_id)
    assert ok is True, reason
    store_id = envelope.payload["store_id"]
    for fn, args in (
        (engine.shop_service.stock_shop, (world_id, "agent_zhangming", store_id, "wheat")),
        (engine.shop_service.adjust_price, (world_id, "agent_zhangming", store_id, "wheat", 6)),
        (engine.shop_service.close_shop, (world_id, "agent_zhangming", store_id)),
    ):
        ok, _, reason = fn(*args, reason="别人的店")
        assert ok is False and reason == MSG_NOT_OWNER
    # 不存在的店
    ok, _, reason = engine.shop_service.close_shop(
        world_id, "agent_linxia", "store_nope", reason="收摊"
    )
    assert ok is False and reason == MSG_STORE_NOT_FOUND


# --------------------------------------------------------------------------- #
# Buy settlement (R41 parallel path)
# --------------------------------------------------------------------------- #


def test_buy_from_personal_shop_settlement(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = _open_stall(engine, world_id)
    assert ok is True, reason
    store_id = envelope.payload["store_id"]
    # 顾客站在摊位 → 按个人店价成交（小麦 6，与杂货店同价）
    place_agent(engine, world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)
    before_shop = stock_row(engine, world_id, "village_shop")
    ok, envelope, reason = engine.economy_service.buy(
        world_id, "agent_zhangming", "wheat", quantity=1, reason="买小麦",
        trace_id="trc_personal_buy",
    )
    assert ok is True, reason
    assert envelope.type == "item_purchased"
    assert envelope.payload["store_id"] == store_id

    # 顾客扣款、店主入账（同一 trace_id 双流水）
    buyer = agent_row(engine, world_id, "agent_zhangming")
    owner = agent_row(engine, world_id, "agent_linxia")
    assert buyer.money == 2994  # 3000 - 6
    assert owner.money == 200 + 6
    buyer_txs = transaction_rows(engine, world_id, "agent_zhangming")
    owner_txs = transaction_rows(engine, world_id, "agent_linxia")
    assert len(buyer_txs) == 1 and buyer_txs[0].type == "expense"
    assert buyer_txs[0].amount == -6
    assert len(owner_txs) == 1 and owner_txs[0].type == "sale_income"
    assert owner_txs[0].amount == 6
    assert owner_txs[0].trace_id == buyer_txs[0].trace_id != ""

    events = engine.events_after(world_id, 0)
    sale = next(e for e in events if e.type == "store_sale_completed")
    assert sale.payload == {
        "store_id": store_id,
        "owner_agent_id": "agent_linxia",
        "buyer_agent_id": "agent_zhangming",
        "item_id": "wheat",
        "item_name": "小麦",
        "quantity": 1,
        "unit_price": 6,
        "total": 6,
    }
    owner_money = next(
        e
        for e in events
        if e.type == "money_changed" and e.payload["agent_id"] == "agent_linxia"
    )
    assert owner_money.payload["amount"] == 6
    assert owner_money.payload["balance"] == 206

    # 个人店售出计入 R18.2（+1 且价格 +1）；杂货店 Stock 不动
    personal = stock_row(engine, world_id, store_id)
    assert personal.day_business == 1
    assert personal.price == STORE_STOCK_INITIAL_PRICE + 1
    assert before_shop.day_business == 0
    assert before_shop.price == 20
    assert stock_row(engine, world_id, "village_shop").price == 20


def test_buy_prefers_store_at_location(engine: WorldEngine) -> None:
    """village_shop 与个人店同卖面包：顾客在个人店 → 按个人店价成交。"""
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "bread", 10)
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "bread", "price": 12}], reason="卖面包",
    )
    assert ok is True, reason
    store_id = envelope.payload["store_id"]

    place_agent(engine, world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)
    ok, envelope, reason = engine.economy_service.buy(
        world_id, "agent_zhangming", "bread", quantity=1, reason="买面包"
    )
    assert ok is True, reason
    assert envelope.payload["unit_price"] == 12
    assert envelope.payload["store_id"] == store_id
    assert agent_row(engine, world_id, "agent_zhangming").money == 2988  # 3000 - 12

    session = SessionLocal()
    try:
        personal = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": store_id, "item_id": "bread"},
        )
        assert personal.stock == STALL_INITIAL_STOCK - 1
        village = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"},
        )
        assert village.stock == 20  # 杂货店货架不动
    finally:
        session.close()


def test_concurrent_last_item_race_personal_shop(engine: WorldEngine) -> None:
    """R4: two buyers racing for a personal shop's last item — one wins."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 1)  # stock = min(1, 5) = 1
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id, "agent_linxia", {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6}], reason="摆摊",
    )
    assert ok is True, reason
    store_id = envelope.payload["store_id"]
    place_agent(engine, world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)

    results: list[tuple[bool, str | None]] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()
        ok, _, reason = engine.economy_service.buy(
            world_id, "agent_zhangming", "wheat", quantity=1, reason="抢最后一单"
        )
        results.append((ok, reason))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for ok, _ in results if ok) == 1, results
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": store_id, "item_id": "wheat"},
        )
        assert product.stock == 0
    finally:
        session.close()
    assert agent_row(engine, world_id, "agent_zhangming").money == 2994  # 恰一单


# --------------------------------------------------------------------------- #
# R15/R40 exclusion
# --------------------------------------------------------------------------- #


def test_personal_shop_no_restock_no_promo(engine: WorldEngine) -> None:
    """个人店在开门时刻不自动补货、不被 M12 促销改写价格。"""
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = _open_stall(engine, world_id)
    assert ok is True, reason
    store_id = envelope.payload["store_id"]

    # 从 08:00 前进到次日 06:00（摊位开门时刻；杂货店 8 点开不受影响）
    advance_minutes(engine, world_id, 1320)

    events = engine.events_after(world_id, 0)
    assert not any(
        e.type == "store_restocked" and e.payload["store_id"] == store_id
        for e in events
    )
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": store_id, "item_id": "wheat"},
        )
        assert product.stock == STALL_INITIAL_STOCK  # 无魔法补货
        assert product.sell_price == 6  # 促销未改写
        assert product.base_sell_price == 6
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Full autonomous chain (fake provider)
# --------------------------------------------------------------------------- #


class _StoreAwareProvider(FakeDecisionProvider):
    """Scripted provider that fills a "$store" placeholder in tool arguments
    with the agent's own store id from the observation's 店铺经营摘要 line."""

    async def decide(self, *, observation: str, context, trace_id: str):
        result = await super().decide(
            observation=observation, context=context, trace_id=trace_id
        )
        if result.tool_name in ("stock_shop", "adjust_price", "close_shop"):
            args = dict(result.tool_arguments or {})
            store_id = args.get("store_id")
            if not store_id or store_id == "$store":
                match = re.search(r"\((store_[0-9a-f]+)\)", observation)
                if match is not None:
                    args["store_id"] = match.group(1)
                    result.tool_arguments = args
        return result


def test_autonomous_shop_chain(world_config: ParsedWorldConfig) -> None:
    """LLM 决策 → 工具 → ShopService 全链路：开店→补货→调价→收摊。"""
    eng = make_engine(
        world_config,
        wire_decisions=True,
        provider=_StoreAwareProvider(
            scripts={
                "agent_linxia": [
                    (
                        "open_shop",
                        {
                            "location": {"stall_id": STALL_1},
                            "products": [{"item_id": "wheat", "price": 6}],
                            "reason": "摆摊卖小麦",
                        },
                    ),
                    ("stock_shop", {"store_id": "$store", "item_id": "wheat", "quantity": 3, "reason": "补货"}),
                    ("adjust_price", {"store_id": "$store", "item_id": "wheat", "new_price": 6, "reason": "涨价"}),
                    ("close_shop", {"store_id": "$store", "reason": "收摊"}),
                ]
            }
        ),
    )
    runtime = eng.create_world("创业世界", autonomous=True)
    world_id = runtime.world_id
    place_agent(eng, world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(eng, world_id, "agent_linxia", money=200)
    add_inventory(eng, world_id, "agent_linxia", "wheat", 10)

    advance_minutes(eng, world_id, 120)  # 初始决策 +2..6，之后每步 +30

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun)
            .where(LLMRun.world_id == world_id, LLMRun.agent_id == "agent_linxia")
            .order_by(LLMRun.world_time, LLMRun.created_at, LLMRun.run_id)
        ).all()
        tools = [run.tool_name for run in runs]
        assert tools[:4] == ["open_shop", "stock_shop", "adjust_price", "close_shop"], tools
        assert all(run.success == 1 for run in runs[:4])
        # 收摊后货架货物退回背包
        assert inventory_of(eng, world_id, "agent_linxia") == {"wheat": 10}
    finally:
        session.close()
    eng._runtimes.clear()
