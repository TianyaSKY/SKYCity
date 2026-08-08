"""M19 tests: personal stores may buy from residents (owner pays from own
balance) — open_shop with buy_price, set_buy_price, and the owner-settlement
branch in EconomyService.sell (mirror of the R41 sale settlement).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.stores import StoreProduct
from app.database.models.transactions import Transaction
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.economy_service import (
    MSG_NOT_BUYABLE,
    MSG_OWNER_POOR,
    MSG_SELL_TO_SELF,
    MSG_STORE_FULL,
    EconomyService,
)
from app.services.shop_service import (
    MSG_BUY_PRICE_OUT_OF_RANGE,
    MSG_NOT_OWNER,
    ShopService,
)
from app.services.stock_service import StockService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_entrepreneurship import (
    STALL_1,
    STALL_1_ANCHOR,
    add_inventory,
    place_agent,
    set_agent,
)
from tests.test_world_engine import agent_row


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture()
def engine(world_config: ParsedWorldConfig) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.economy_service = EconomyService(eng, SessionLocal)
    eng.stock_service = StockService(eng, SessionLocal)
    eng.shop_service = ShopService(eng, SessionLocal)
    yield eng
    eng._runtimes.clear()


def _open_stall_with_buy(
        engine: WorldEngine,
        world_id: str,
        agent_id: str = "agent_linxia",
        buy_price: int = 3,
) -> str:
    place_agent(engine, world_id, agent_id, STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, agent_id, money=200)
    add_inventory(engine, world_id, agent_id, "wheat", 10)
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id,
        agent_id,
        {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6, "buy_price": buy_price}],
        reason="摆摊兼收购",
    )
    assert ok is True and reason is None
    return envelope.payload["store_id"]


def _product(engine: WorldEngine, world_id: str, store_id: str) -> StoreProduct:
    session = SessionLocal()
    try:
        return session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": store_id, "item_id": "wheat"},
        )
    finally:
        session.close()


def _inventory(engine: WorldEngine, world_id: str, agent_id: str) -> dict[str, int]:
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


def _txs(engine: WorldEngine, world_id: str, agent_id: str) -> list[Transaction]:
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


# --------------------------------------------------------------------------- #
# open_shop with buy_price
# --------------------------------------------------------------------------- #


def test_open_shop_sets_buy_price(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    store_id = _open_stall_with_buy(engine, world_id, buy_price=3)
    assert _product(engine, world_id, store_id).buy_price == 3


def test_open_shop_defaults_buy_price_zero(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id,
        "agent_linxia",
        {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6}],
        reason="摆摊",
    )
    assert ok is True and reason is None
    assert _product(engine, world_id, envelope.payload["store_id"]).buy_price == 0


def test_open_shop_rejects_buy_price_above_base(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", money=200)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 10)
    ok, envelope, reason = engine.shop_service.open_shop(
        world_id,
        "agent_linxia",
        {"stall_id": STALL_1},
        [{"item_id": "wheat", "price": 6, "buy_price": 10}],  # 收购价上限 min(3, 4)=3
        reason="摆摊",
    )
    assert ok is False and envelope is None
    assert reason == MSG_BUY_PRICE_OUT_OF_RANGE


# --------------------------------------------------------------------------- #
# set_buy_price
# --------------------------------------------------------------------------- #


def test_set_buy_price_updates_and_publishes(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    store_id = _open_stall_with_buy(engine, world_id, buy_price=0)

    ok, envelope, reason = engine.shop_service.set_buy_price(
        world_id, "agent_linxia", store_id, "wheat", 2, reason="开始收购"
    )
    assert ok is True and reason is None
    assert envelope.type == "store_buy_price_changed"
    assert _product(engine, world_id, store_id).buy_price == 2


def test_set_buy_price_rejects_non_owner(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    store_id = _open_stall_with_buy(engine, world_id)

    ok, _, reason = engine.shop_service.set_buy_price(
        world_id, "agent_zhangming", store_id, "wheat", 2, reason="捣乱"
    )
    assert ok is False and reason == MSG_NOT_OWNER


def test_set_buy_price_rejects_out_of_range(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    store_id = _open_stall_with_buy(engine, world_id)

    ok, _, reason = engine.shop_service.set_buy_price(
        world_id, "agent_linxia", store_id, "wheat", 99, reason="乱定价"
    )
    assert ok is False and reason == MSG_BUY_PRICE_OUT_OF_RANGE


# --------------------------------------------------------------------------- #
# sell to a personal store (owner pays)
# --------------------------------------------------------------------------- #


def test_sell_to_personal_store_settles_from_owner(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    store_id = _open_stall_with_buy(engine, world_id, buy_price=3)
    # seller arrives at the stall with 5 wheat
    place_agent(engine, world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)
    add_inventory(engine, world_id, "agent_zhangming", "wheat", 5)

    ok, envelope, reason = engine.economy_service.sell(
        world_id, "agent_zhangming", "wheat", quantity=2, reason="卖小麦给林夏的摊"
    )
    assert ok is True and reason is None
    assert envelope.type == "item_sold"
    assert envelope.payload["unit_price"] == 3 and envelope.payload["total"] == 6

    # seller: +6 coins, 3 wheat left
    seller = agent_row(engine, world_id, "agent_zhangming")
    assert seller.money == 3006  # 3000 + 6
    assert _inventory(engine, world_id, "agent_zhangming") == {"wheat": 3}
    # owner: -6 coins, shelf +2
    owner = agent_row(engine, world_id, "agent_linxia")
    assert owner.money == 200 - 6
    assert _product(engine, world_id, store_id).stock == 5 + 2  # 5 initial + 2 bought
    # purchase event + owner expense transaction
    purchase = [
        e for e in engine.events_after(world_id, 0)
        if e.type == "store_purchase_completed"
    ]
    assert purchase and purchase[0].payload["total"] == 6
    owner_txs = _txs(engine, world_id, "agent_linxia")
    assert owner_txs[-1].type == "expense" and owner_txs[-1].amount == -6


def test_sell_to_personal_store_rejects_own_goods(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    _open_stall_with_buy(engine, world_id, buy_price=3)

    ok, _, reason = engine.economy_service.sell(
        world_id, "agent_linxia", "wheat", quantity=1, reason="自己卖给自己"
    )
    assert ok is False and reason == MSG_SELL_TO_SELF


def test_sell_to_personal_store_rejects_poor_owner(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    store_id = _open_stall_with_buy(engine, world_id, buy_price=3)
    set_agent(engine, world_id, "agent_linxia", money=2)  # cannot cover 3×2

    place_agent(engine, world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)
    add_inventory(engine, world_id, "agent_zhangming", "wheat", 5)

    ok, _, reason = engine.economy_service.sell(
        world_id, "agent_zhangming", "wheat", quantity=2, reason="卖小麦"
    )
    assert ok is False and reason == MSG_OWNER_POOR
    # nothing moved: seller keeps goods, shelf unchanged
    assert _inventory(engine, world_id, "agent_zhangming") == {"wheat": 5}
    assert _product(engine, world_id, store_id).stock == 5


def test_sell_to_personal_store_respects_shelf_cap(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    store_id = _open_stall_with_buy(engine, world_id, buy_price=3)
    # fill the shelf to the cap (STALL_STOCK_CAP = 20)
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": store_id, "item_id": "wheat"},
        )
        product.stock = 20
        session.commit()
    finally:
        session.close()

    place_agent(engine, world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)
    add_inventory(engine, world_id, "agent_zhangming", "wheat", 5)

    ok, _, reason = engine.economy_service.sell(
        world_id, "agent_zhangming", "wheat", quantity=2, reason="卖小麦"
    )
    assert ok is False and reason == MSG_STORE_FULL


def test_personal_store_buy_price_zero_still_rejects(engine: WorldEngine) -> None:
    """Default buy_price=0 keeps the M18 '只卖不收' behaviour."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    _open_stall_with_buy(engine, world_id, buy_price=0)

    place_agent(engine, world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)
    add_inventory(engine, world_id, "agent_zhangming", "wheat", 5)

    ok, _, reason = engine.economy_service.sell(
        world_id, "agent_zhangming", "wheat", quantity=1, reason="卖小麦"
    )
    assert ok is False and reason == MSG_NOT_BUYABLE
