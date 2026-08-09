"""Cooperative-stall tests.

Residents may operate one personal shelf at one fixed map stall.  The tests
exercise the public contract end to end: opening moves the permit fee to the
common treasury, a resident can sell from the stall, closing returns shelf
stock without mutating the map or cooperative-share issues, and invalid
locations/duplicate stalls/insufficient funds are rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.gameplay import STALL_INITIAL_STOCK, STALL_PERMIT_FEE, STALL_STOCK_CAP
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.locations import WorldLocation
from app.database.models.stocks import Stock
from app.database.models.stores import Store, StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.economy_service import EconomyService
from app.services.shop_service import (
    MSG_ALREADY_HAS_STALL,
    MSG_NOT_AT_STALL,
    MSG_NOT_OWNER,
    MSG_PRICE_OUT_OF_RANGE,
    MSG_STALL_FEE_UNAFFORDABLE,
    MSG_STALL_OCCUPIED,
    MSG_STORE_FULL,
    MSG_STORE_NOT_FOUND,
    ShopService,
)
from app.services.stock_service import StockService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_world_engine import advance_minutes

STALL_1 = "stall_plaza_1"
STALL_2 = "stall_plaza_2"
STALL_1_ANCHOR = (30, 19)
STALL_2_ANCHOR = (33, 19)


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


def make_engine(
    world_config: ParsedWorldConfig,
    scripts=None,
    wire_decisions: bool = False,
) -> WorldEngine:
    engine = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    engine.action_service = ActionExecutionService(engine, SessionLocal)
    engine.economy_service = EconomyService(engine, SessionLocal)
    engine.stock_service = StockService(engine, SessionLocal)
    engine.shop_service = ShopService(engine, SessionLocal)
    if wire_decisions:
        engine.decision_service = DecisionService(
            engine, SessionLocal, provider=FakeDecisionProvider(scripts=scripts)
        )
    return engine


@pytest.fixture()
def engine(world_config: ParsedWorldConfig) -> WorldEngine:
    instance = make_engine(world_config)
    yield instance
    instance._runtimes.clear()


def place_agent(
    world_id: str, agent_id: str, location_id: str | None, col: int, row: int
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


def set_agent(world_id: str, agent_id: str, **fields: object) -> None:
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert agent is not None
        for key, value in fields.items():
            setattr(agent, key, value)
        session.commit()
    finally:
        session.close()


def add_inventory(world_id: str, agent_id: str, item_id: str, quantity: int) -> None:
    session = SessionLocal()
    try:
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
        session.commit()
    finally:
        session.close()


def inventory_of(world_id: str, agent_id: str) -> dict[str, int]:
    session = SessionLocal()
    try:
        return {
            row.item_id: row.quantity
            for row in session.scalars(
                select(Inventory).where(
                    Inventory.world_id == world_id,
                    Inventory.agent_id == agent_id,
                )
            )
        }
    finally:
        session.close()


def agent_money(world_id: str, agent_id: str) -> int:
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert agent is not None
        return agent.money
    finally:
        session.close()


def treasury(world_id: str) -> int:
    session = SessionLocal()
    try:
        world = session.get(World, world_id)
        assert world is not None
        return world.treasury
    finally:
        session.close()


def personal_stores(world_id: str) -> list[Store]:
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
            )
        )
    finally:
        session.close()


def locations_by_id(world_id: str) -> dict[str, WorldLocation]:
    session = SessionLocal()
    try:
        return {
            location.location_id: location
            for location in session.scalars(
                select(WorldLocation).where(WorldLocation.world_id == world_id)
            )
        }
    finally:
        session.close()


def stock_ids(world_id: str) -> list[str]:
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(Stock.stock_id)
                .where(Stock.world_id == world_id)
                .order_by(Stock.stock_id)
            )
        )
    finally:
        session.close()


def open_stall(
    engine: WorldEngine,
    world_id: str,
    agent_id: str = "agent_linxia",
    stall_id: str = STALL_1,
    money: int = 100,
    item_id: str = "wheat",
    price: int = 6,
    quantity: int = 10,
):
    anchor = STALL_1_ANCHOR if stall_id == STALL_1 else STALL_2_ANCHOR
    place_agent(world_id, agent_id, stall_id, *anchor)
    set_agent(world_id, agent_id, money=money)
    add_inventory(world_id, agent_id, item_id, quantity)
    return engine.shop_service.open_shop(
        world_id,
        agent_id,
        stall_id,
        [{"item_id": item_id, "price": price}],
        reason="摆摊",
    )


# --------------------------------------------------------------------------- #
# Fixed-stall eligibility and opening ledger
# --------------------------------------------------------------------------- #


def test_opening_at_fixed_stall_charges_treasury_and_creates_no_share(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    treasury_before = treasury(world_id)
    shares_before = stock_ids(world_id)
    locations_before = locations_by_id(world_id)

    ok, envelope, reason = open_stall(engine, world_id)

    assert ok is True
    assert reason is None
    assert envelope.type == "store_opened"
    assert envelope.payload["location_id"] == STALL_1
    assert envelope.payload["stall_id"] == STALL_1
    assert (envelope.payload["col"], envelope.payload["row"]) == STALL_1_ANCHOR
    assert envelope.payload["stall_fee"] == STALL_PERMIT_FEE
    assert envelope.payload["treasury_balance"] == treasury_before + STALL_PERMIT_FEE
    assert envelope.payload["products"] == [
        {"item_id": "wheat", "sell_price": 6, "buy_price": 0, "stock": STALL_INITIAL_STOCK}
    ]

    store = personal_stores(world_id)[0]
    assert store.owner_agent_id == "agent_linxia"
    assert store.location_id == STALL_1
    assert store.company_id is None
    assert store.name == "林夏的合作社摊"
    assert agent_money(world_id, "agent_linxia") == 100 - STALL_PERMIT_FEE
    assert treasury(world_id) == treasury_before + STALL_PERMIT_FEE
    assert inventory_of(world_id, "agent_linxia") == {"wheat": 5}
    assert stock_ids(world_id) == shares_before
    assert set(locations_by_id(world_id)) == set(locations_before)

    session = SessionLocal()
    try:
        permit = session.scalar(
            select(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.agent_id == "agent_linxia",
                Transaction.type == "stall_permit_fee",
            )
        )
        assert permit is not None
        assert permit.amount == -STALL_PERMIT_FEE
        product = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": store.store_id, "item_id": "wheat"},
        )
        assert product is not None
        assert (product.stock, product.stock_cap, product.restock_daily) == (
            STALL_INITIAL_STOCK,
            STALL_STOCK_CAP,
            0,
        )
    finally:
        session.close()


def test_opening_rejects_arbitrary_location_second_stall_and_insufficient_fee(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(world_id, "agent_linxia", money=100)
    add_inventory(world_id, "agent_linxia", "wheat", 10)

    assert engine.shop_service.open_shop(
        world_id,
        "agent_linxia",
        "village_plaza",
        [{"item_id": "wheat", "price": 6}],
        reason="任意坐标摆摊",
    ) == (False, None, MSG_NOT_AT_STALL)

    assert open_stall(engine, world_id)[0]
    place_agent(world_id, "agent_linxia", STALL_2, *STALL_2_ANCHOR)
    add_inventory(world_id, "agent_linxia", "wheat", 10)
    assert engine.shop_service.open_shop(
        world_id,
        "agent_linxia",
        STALL_2,
        [{"item_id": "wheat", "price": 6}],
        reason="第二摊",
    ) == (False, None, MSG_ALREADY_HAS_STALL)

    runtime = engine.create_world()
    poor_world_id = runtime.world_id
    place_agent(poor_world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(poor_world_id, "agent_linxia", money=STALL_PERMIT_FEE - 1)
    add_inventory(poor_world_id, "agent_linxia", "wheat", 10)
    assert engine.shop_service.open_shop(
        poor_world_id,
        "agent_linxia",
        STALL_1,
        [{"item_id": "wheat", "price": 6}],
        reason="没钱摆摊",
    ) == (False, None, MSG_STALL_FEE_UNAFFORDABLE)
    assert personal_stores(poor_world_id) == []


def test_fixed_stall_accepts_only_one_resident(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    assert open_stall(engine, world_id)[0]
    assert open_stall(engine, world_id, agent_id="agent_zhangming", money=100)[2] == MSG_STALL_OCCUPIED
    assert len(personal_stores(world_id)) == 1


# --------------------------------------------------------------------------- #
# Operation, price anchor, and clean closure
# --------------------------------------------------------------------------- #


def test_resident_can_operate_and_close_stall_without_mutating_map_or_shares(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    shares_before = stock_ids(world_id)
    locations_before = locations_by_id(world_id)
    ok, opened, reason = open_stall(engine, world_id)
    assert ok is True, reason
    store_id = opened.payload["store_id"]

    place_agent(world_id, "agent_zhangming", STALL_1, *STALL_1_ANCHOR)
    buyer_before = agent_money(world_id, "agent_zhangming")
    owner_before = agent_money(world_id, "agent_linxia")
    ok, purchased, reason = engine.economy_service.buy(
        world_id, "agent_zhangming", "wheat", quantity=1, reason="买小麦"
    )
    assert ok is True, reason
    assert purchased.payload["store_id"] == store_id
    assert purchased.payload["unit_price"] == 6
    assert agent_money(world_id, "agent_zhangming") == buyer_before - 6
    assert agent_money(world_id, "agent_linxia") == owner_before + 6

    ok, closed, reason = engine.shop_service.close_shop(
        world_id, "agent_linxia", store_id, reason="收摊"
    )
    assert ok is True, reason
    assert closed.type == "store_closed"
    assert personal_stores(world_id) == []
    # Initial 10: five went to the shelf, one sold, four returned on close.
    assert inventory_of(world_id, "agent_linxia") == {"wheat": 9}
    assert stock_ids(world_id) == shares_before
    locations_after = locations_by_id(world_id)
    assert set(locations_after) == set(locations_before)
    assert locations_after[STALL_1].location_type == "stall"


def test_stall_keeps_village_price_anchor_and_owner_controls(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    assert open_stall(engine, world_id)[0]
    store_id = personal_stores(world_id)[0].store_id

    assert engine.shop_service.adjust_price(
        world_id, "agent_linxia", store_id, "wheat", 5, reason="压价"
    ) == (False, None, MSG_PRICE_OUT_OF_RANGE)
    ok, envelope, reason = engine.shop_service.adjust_price(
        world_id, "agent_linxia", store_id, "wheat", 6, reason="维持锚价"
    )
    assert ok is True
    assert reason is None
    assert envelope.payload["sell_price"] == 6
    assert engine.shop_service.adjust_price(
        world_id, "agent_zhangming", store_id, "wheat", 6, reason="越权"
    ) == (False, None, MSG_NOT_OWNER)

    add_inventory(world_id, "agent_linxia", "wheat", 3)
    ok, envelope, reason = engine.shop_service.stock_shop(
        world_id, "agent_linxia", store_id, "wheat", quantity=3, reason="补货"
    )
    assert ok is True
    assert reason is None
    assert envelope.payload["stock_after"] == STALL_INITIAL_STOCK + 3
    assert engine.shop_service.close_shop(
        world_id, "agent_linxia", "store_missing", reason="收摊"
    ) == (False, None, MSG_STORE_NOT_FOUND)


def test_stall_stock_capacity_is_enforced(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    assert open_stall(engine, world_id, quantity=20)[0]
    store_id = personal_stores(world_id)[0].store_id
    # Shelf starts with five; move fifteen from the backpack to reach capacity.
    assert engine.shop_service.stock_shop(
        world_id, "agent_linxia", store_id, "wheat", quantity=15, reason="补满"
    )[0]
    add_inventory(world_id, "agent_linxia", "wheat", 1)
    assert engine.shop_service.stock_shop(
        world_id, "agent_linxia", store_id, "wheat", quantity=1, reason="超额"
    ) == (False, None, MSG_STORE_FULL)


# --------------------------------------------------------------------------- #
# Decision-tool contract
# --------------------------------------------------------------------------- #


def test_llm_open_shop_uses_stall_id_contract(world_config: ParsedWorldConfig) -> None:
    engine = make_engine(
        world_config,
        scripts={
            "agent_linxia": [
                (
                    "open_shop",
                    {
                        "stall_id": STALL_1,
                        "products": [{"item_id": "wheat", "price": 6}],
                        "reason": "摆摊卖小麦",
                    },
                )
            ]
        },
        wire_decisions=True,
    )
    runtime = engine.create_world("摊位决策", autonomous=True)
    world_id = runtime.world_id
    place_agent(world_id, "agent_linxia", STALL_1, *STALL_1_ANCHOR)
    set_agent(world_id, "agent_linxia", money=100)
    add_inventory(world_id, "agent_linxia", "wheat", 10)

    advance_minutes(engine, world_id, 20)

    stores = personal_stores(world_id)
    assert len(stores) == 1
    assert stores[0].location_id == STALL_1
    engine._runtimes.clear()
