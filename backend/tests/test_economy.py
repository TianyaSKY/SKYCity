"""M5 tests: economy — needs ticking (R14), work lifecycle (R10), buy/sell/use
with the R4 atomic race guard, restock (R15), and the full autonomous chain.

Drives the WorldEngine directly (no HTTP, no background loop): clock advanced
via tick + engine._tick_runtime, exactly like test_world_engine.py.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.agents.providers.fake_provider import DEFAULT_SCRIPTS, FakeDecisionProvider
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.jobs import Employment
from app.database.models.llm_runs import LLMRun
from app.database.models.stores import StoreProduct
from app.database.models.transactions import Transaction
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.economy_service import (
    MSG_BUSY,
    MSG_EXHAUSTED,
    MSG_HUNGRY_FULL,
    MSG_LOCATION_CLOSED,
    MSG_NO_MONEY,
    MSG_NO_STOCK,
    MSG_NOT_AT_JOB,
    MSG_NOT_AT_STORE,
    MSG_NOT_BUYABLE,
    MSG_NOT_FOOD,
    MSG_NOT_IN_INVENTORY,
    MSG_STORE_CLOSED,
    MSG_STORE_FULL,
    EconomyService,
)
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine

from tests.test_world_engine import advance_minutes, agent_row

SHOP_ANCHOR = (23, 12)
FARM_ANCHOR = (47, 24)


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


def make_engine(
    world_config: ParsedWorldConfig, scripts=None, wire_decisions: bool = False
) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.economy_service = EconomyService(eng, SessionLocal)
    if wire_decisions:
        eng.decision_service = DecisionService(
            eng, SessionLocal, provider=FakeDecisionProvider(scripts=scripts)
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


def set_stock(engine: WorldEngine, world_id: str, item_id: str, stock: int) -> None:
    session = SessionLocal()
    try:
        session.execute(
            update(StoreProduct)
            .where(
                StoreProduct.world_id == world_id,
                StoreProduct.store_id == "village_shop",
                StoreProduct.item_id == item_id,
            )
            .values(stock=stock)
        )
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


# --------------------------------------------------------------------------- #
# R14 hourly needs tick
# --------------------------------------------------------------------------- #


def test_hourly_needs_tick_and_idempotency(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", hunger=10, energy=50)

    advance_minutes(engine, world_id, 61)  # 480 -> 541: one hour boundary

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.hunger == 11  # +1/h
    assert row.energy == 49  # -1/h

    # idempotent within the same hour
    advance_minutes(engine, world_id, 5)
    row = agent_row(engine, world_id, "agent_linxia")
    assert (row.hunger, row.energy) == (11, 49)

    envelopes = engine.events_after(world_id, 0)
    needs = [
        e
        for e in envelopes
        if e.type == "needs_changed" and e.payload["agent_id"] == "agent_linxia"
    ]
    assert needs, "needs_changed must be emitted on the hourly tick"
    assert needs[0].payload == {
        "agent_id": "agent_linxia",
        "hunger": 11,
        "energy": 49,
        "mood": 99,
    }


def test_hourly_wait_recovers_energy(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(
        engine, world_id, "agent_linxia", hunger=0, energy=5, action_type="wait"
    )

    advance_minutes(engine, world_id, 61)  # crosses 540: one hour boundary

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.hunger == 1
    assert row.energy == 9  # -1 then +5 (wait recovery)


def test_hourly_tick_hunger_100_extra_drain(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", hunger=100, energy=10)

    advance_minutes(engine, world_id, 61)

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.hunger == 100  # clamped
    assert row.energy == 8  # -1 hourly and -1 extra for hunger == 100 (R11)


# --------------------------------------------------------------------------- #
# Work (R10/R11/R12)
# --------------------------------------------------------------------------- #


def test_work_rejected_wrong_location(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.economy_service.work_start(
        runtime.world_id, "agent_linxia", "job_farm_field", reason="干活"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_AT_JOB  # linxia is at home, the job is at the farm


def test_work_rejected_closed_location(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)
    advance_minutes(engine, world_id, 620)  # 480 -> 1100 (farm closes 18:00)

    ok, envelope, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干活"
    )
    assert ok is False and envelope is None
    assert reason == MSG_LOCATION_CLOSED


def test_work_rejected_busy(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", action_type="wait")

    ok, envelope, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干活"
    )
    assert ok is False and envelope is None
    assert reason == MSG_BUSY  # R1/R3: one action at a time


def test_work_rejected_hunger_full(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", hunger=100)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干活"
    )
    assert ok is False
    assert reason == MSG_HUNGRY_FULL  # R11


def test_work_rejected_exhausted(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", energy=0)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干活"
    )
    assert ok is False
    assert reason == MSG_EXHAUSTED  # R12


def test_work_lifecycle_settles_at_completion(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)

    ok, envelope, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干农活"
    )
    assert ok is True and reason is None
    assert envelope.type == "work_started"
    assert envelope.payload["job_id"] == "job_farm_field"
    assert envelope.payload["job_name"] == "农场劳作"
    assert envelope.payload["duration_minutes"] == 120
    assert envelope.payload["ends_at"] == 600

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.action_type == "work"
    assert row.action_data == {"job_id": "job_farm_field", "reason": "干农活"}

    # Second action while working is rejected (R3: work is exclusive).
    ok, _, reason = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=1, reason="买东西"
    )
    assert ok is False and reason == MSG_BUSY

    advance_minutes(engine, world_id, 121)  # 480 -> 601; 2 hourly ticks + completion

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.action_type is None
    assert row.money == 80  # 50 + wage 30 (R10)
    # energy: -1/h x2 (hourly) then -8 work drain (4/h x 2h) = 90
    assert row.energy == 90
    assert row.hunger == 2  # +1/h x2 (R14)

    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 1}

    session = SessionLocal()
    try:
        employment = session.get(
            Employment,
            {"world_id": world_id, "agent_id": "agent_linxia", "job_id": "job_farm_field"},
        )
        assert employment is not None
        assert employment.hours_worked == pytest.approx(2.0)
        assert employment.total_earned == 30
    finally:
        session.close()

    txs = transaction_rows(engine, world_id, "agent_linxia")
    assert len(txs) == 1
    assert txs[0].type == "work_wage"
    assert txs[0].amount == 30
    assert txs[0].balance_after == 80

    types = [e.type for e in engine.events_after(world_id, 0)]
    assert "work_started" in types
    completed = [e for e in engine.events_after(world_id, 0) if e.type == "work_completed"]
    assert completed
    assert completed[0].payload["wage"] == 30
    assert completed[0].payload["products"] == [{"item_id": "wheat", "quantity": 1}]
    assert completed[0].payload["energy_spent"] == 8
    money_events = [e for e in engine.events_after(world_id, 0) if e.type == "money_changed"]
    assert money_events[-1].payload == {
        "agent_id": "agent_linxia",
        "amount": 30,
        "balance": 80,
        "reason": "完成工作 农场劳作 获得工资",
    }
    inv_events = [e for e in engine.events_after(world_id, 0) if e.type == "inventory_changed"]
    assert inv_events and inv_events[-1].payload["items"] == [{"item_id": "wheat", "quantity": 1}]


# --------------------------------------------------------------------------- #
# Buy (R4/R7/R8)
# --------------------------------------------------------------------------- #


def test_buy_success_and_snapshot_inventory(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)

    ok, envelope, reason = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=1, reason="买面包"
    )
    assert ok is True and reason is None
    assert envelope.type == "item_purchased"
    assert envelope.payload == {
        "agent_id": "agent_linxia",
        "item_id": "bread",
        "item_name": "面包",
        "quantity": 1,
        "unit_price": 12,
        "total": 12,
    }

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 38
    assert inventory_of(engine, world_id, "agent_linxia") == {"bread": 1}

    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product.stock == 19
    finally:
        session.close()

    txs = transaction_rows(engine, world_id, "agent_linxia")
    assert txs[0].type == "expense"
    assert txs[0].amount == -12
    assert txs[0].balance_after == 38
    assert txs[0].item_id == "bread"

    events = engine.events_after(world_id, 0)
    assert any(
        e.type == "money_changed"
        and e.payload == {
            "agent_id": "agent_linxia",
            "amount": -12,
            "balance": 38,
            "reason": "购买 面包×1",
        }
        for e in events
    )
    assert any(
        e.type == "inventory_changed" and e.payload["items"] == [{"item_id": "bread", "quantity": 1}]
        for e in events
    )

    # Contract: snapshot inventory is always an array of {item_id, quantity}.
    snapshot = engine.snapshot(world_id)
    linxia = next(a for a in snapshot["agents"] if a["agent_id"] == "agent_linxia")
    assert linxia["inventory"] == [{"item_id": "bread", "quantity": 1}]
    other = next(a for a in snapshot["agents"] if a["agent_id"] == "agent_zhangming")
    assert other["inventory"] == []


def test_buy_rejected_no_money(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)

    ok, envelope, reason = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=5, reason="囤货"
    )  # 5 * 12 = 60 > 50
    assert ok is False and envelope is None
    assert reason == MSG_NO_MONEY  # R7: no credit

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 50
    assert inventory_of(engine, world_id, "agent_linxia") == {}
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product.stock == 20  # untouched
    finally:
        session.close()
    assert engine.events_after(world_id, 0) == []  # nothing published


def test_buy_rejected_no_stock(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)
    set_stock(engine, world_id, "bread", 1)

    ok, envelope, reason = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=2, reason="全买走"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NO_STOCK

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 50
    assert inventory_of(engine, world_id, "agent_linxia") == {}
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product.stock == 1
    finally:
        session.close()


def test_buy_rejected_closed_store(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)
    advance_minutes(engine, world_id, 780)  # 480 -> 1260 = 21:00, shop closes 20:00

    ok, envelope, reason = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=1, reason="买面包"
    )
    assert ok is False and envelope is None
    assert reason == MSG_STORE_CLOSED  # R8


def test_buy_rejected_not_at_store(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id  # linxia is at home

    ok, envelope, reason = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=1, reason="买面包"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_AT_STORE


def test_concurrent_last_item_race_exactly_one_wins(engine: WorldEngine) -> None:
    """R4: two buyers racing for the last bread — exactly one succeeds."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)
    set_stock(engine, world_id, "bread", 1)

    results: list[tuple[bool, str | None]] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()  # both threads start the atomic buy together
        ok, _, reason = engine.economy_service.buy(
            world_id, "agent_linxia", "bread", quantity=1, reason="抢最后一个"
        )
        results.append((ok, reason))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert sum(1 for ok, _ in results if ok) == 1, f"expected one winner, got {results}"
    assert sum(1 for ok, reason in results if reason == MSG_NO_STOCK) == 1

    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product.stock == 0
    finally:
        session.close()
    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 38  # exactly one transaction
    assert inventory_of(engine, world_id, "agent_linxia") == {"bread": 1}
    assert len(transaction_rows(engine, world_id, "agent_linxia")) == 1


# --------------------------------------------------------------------------- #
# Sell
# --------------------------------------------------------------------------- #


def test_sell_success(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 1)
    set_stock(engine, world_id, "wheat", 29)  # room under cap 30

    ok, envelope, reason = engine.economy_service.sell(
        world_id, "agent_linxia", "wheat", quantity=1, reason="卖小麦"
    )
    assert ok is True and reason is None
    assert envelope.type == "item_sold"
    assert envelope.payload == {
        "agent_id": "agent_linxia",
        "item_id": "wheat",
        "item_name": "小麦",
        "quantity": 1,
        "unit_price": 3,  # buy_price
        "total": 3,
    }

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 53
    assert inventory_of(engine, world_id, "agent_linxia") == {}  # stack emptied

    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "wheat"}
        )
        assert product.stock == 30
    finally:
        session.close()

    txs = transaction_rows(engine, world_id, "agent_linxia")
    assert txs[0].type == "income"
    assert txs[0].amount == 3
    assert txs[0].balance_after == 53

    events = engine.events_after(world_id, 0)
    assert any(e.type == "item_sold" for e in events)
    assert any(
        e.type == "money_changed"
        and e.payload == {"agent_id": "agent_linxia", "amount": 3, "balance": 53, "reason": "出售 小麦×1"}
        for e in events
    )


def test_sell_rejected_store_full(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)
    add_inventory(engine, world_id, "agent_linxia", "wheat", 1)  # stock already at cap 30

    ok, envelope, reason = engine.economy_service.sell(
        world_id, "agent_linxia", "wheat", quantity=1, reason="卖小麦"
    )
    assert ok is False and envelope is None
    assert reason == MSG_STORE_FULL

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 50
    assert inventory_of(engine, world_id, "agent_linxia") == {"wheat": 1}


def test_sell_rejected_not_buyable(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)
    add_inventory(engine, world_id, "agent_linxia", "tool_rake", 1)  # buy_price == 0

    ok, envelope, reason = engine.economy_service.sell(
        world_id, "agent_linxia", "tool_rake", quantity=1, reason="卖耙子"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_BUYABLE


# --------------------------------------------------------------------------- #
# Use (food)
# --------------------------------------------------------------------------- #


def test_use_food_restores_hunger(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", hunger=50)
    add_inventory(engine, world_id, "agent_linxia", "bread", 2)

    ok, envelope, reason = engine.economy_service.use_item(
        world_id, "agent_linxia", "bread", reason="饿了"
    )
    assert ok is True and reason is None
    assert envelope.type == "item_used"
    assert envelope.payload == {
        "agent_id": "agent_linxia",
        "item_id": "bread",
        "item_name": "面包",
        "hunger_before": 50,
        "hunger_after": 20,
        "mood_before": 100,
        "mood_after": 100,
    }

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.hunger == 20
    assert inventory_of(engine, world_id, "agent_linxia") == {"bread": 1}

    events = engine.events_after(world_id, 0)
    assert any(
        e.type == "needs_changed"
        and e.payload == {"agent_id": "agent_linxia", "hunger": 20, "energy": 100, "mood": 100}
        for e in events
    )
    assert any(
        e.type == "inventory_changed" and e.payload["items"] == [{"item_id": "bread", "quantity": 1}]
        for e in events
    )


def test_use_rejected_not_food(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    add_inventory(engine, world_id, "agent_linxia", "wheat", 1)

    ok, envelope, reason = engine.economy_service.use_item(
        world_id, "agent_linxia", "wheat", reason="试试"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_FOOD


def test_use_rejected_not_in_inventory(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id

    ok, envelope, reason = engine.economy_service.use_item(
        world_id, "agent_linxia", "apple", reason="吃苹果"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_IN_INVENTORY


# --------------------------------------------------------------------------- #
# R15 restock at the daily open hour
# --------------------------------------------------------------------------- #


def test_restock_at_next_day_open_hour(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)

    ok, _, _ = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=1, reason="买一个"
    )
    assert ok is True
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product.stock == 19
    finally:
        session.close()

    advance_minutes(engine, world_id, 1440)  # 480 -> next day 08:00 (1920)

    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product.stock == 20  # min(cap, 19 + restock_daily 10)
    finally:
        session.close()

    restock = [e for e in engine.events_after(world_id, 0) if e.type == "store_restocked"]
    assert restock
    assert restock[0].payload == {
        "store_id": "village_shop",
        "restocked": [{"item_id": "bread", "quantity": 1}],
    }


# --------------------------------------------------------------------------- #
# R12 forced rest guard
# --------------------------------------------------------------------------- #


def test_exhausted_agent_forced_to_rest(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(
        world_config,
        scripts={"agent_linxia": DEFAULT_SCRIPTS["agent_linxia"]},
        wire_decisions=True,
    )
    runtime = eng.create_world("力竭世界", autonomous=True)
    world_id = runtime.world_id
    set_agent(eng, world_id, "agent_linxia", energy=0)

    advance_minutes(eng, world_id, 10)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(
                LLMRun.world_id == world_id, LLMRun.agent_id == "agent_linxia"
            )
        ).all()
        assert runs == [], "no LLM decision while exhausted (R12)"
        row = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert row.action_type == "wait"
        assert row.action_data == {"reason": "精力耗尽，强制休息"}
    finally:
        session.close()

    types = [e.type for e in eng.events_after(world_id, 0)]
    assert "agent_wait_started" in types
    texts = [
        e.payload["text"]
        for e in eng.events_after(world_id, 0)
        if e.type == "world_event_created"
    ]
    assert any("精力耗尽，正在休息" in text for text in texts)
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Full autonomous economy chain (T5-8)
# --------------------------------------------------------------------------- #


def test_full_autonomous_economy_chain(world_config: ParsedWorldConfig) -> None:
    """Linxia: shop -> 4x buy bread (50-48=2) -> 5th buy fails 余额不足 ->
    farm work (+30 wage, +wheat) -> sell wheat (+3) -> buy bread (-12) ->
    eat it. Final balance 23, bread left 4."""
    eng = make_engine(
        world_config,
        scripts={"agent_linxia": DEFAULT_SCRIPTS["agent_linxia"]},
        wire_decisions=True,
    )
    runtime = eng.create_world("经济链", autonomous=True)
    world_id = runtime.world_id
    # The shop starts at full wheat stock (30/30); make room so the sell lands.
    set_stock(eng, world_id, "wheat", 29)

    done = False
    for _ in range(20):  # up to 1200 game minutes
        advance_minutes(eng, world_id, 60)
        row = agent_row(eng, world_id, "agent_linxia")
        used = any(e.type == "item_used" for e in eng.events_after(world_id, 0))
        if row.money == 23 and used:
            done = True
            break
    assert done, "economic chain did not complete in time"

    row = agent_row(eng, world_id, "agent_linxia")
    assert row.money == 23  # 50 - 4*12 + 30 + 3 - 12
    assert inventory_of(eng, world_id, "agent_linxia") == {"bread": 4}

    # item_used carried hunger before/after and actually reduced hunger
    used_events = [
        e for e in eng.events_after(world_id, 0) if e.type == "item_used"
    ]
    assert used_events
    used = used_events[0].payload
    assert used["item_id"] == "bread"
    assert used["hunger_before"] > used["hunger_after"]
    assert used["hunger_after"] == 0
    assert row.hunger < used["hunger_before"]  # hunger stayed low afterwards

    # the failed buy is recorded as a failure llm_run (T3-9 adjustment)
    session = SessionLocal()
    try:
        failed = session.scalars(
            select(LLMRun).where(
                LLMRun.world_id == world_id,
                LLMRun.agent_id == "agent_linxia",
                LLMRun.tool_name == "buy_item",
                LLMRun.success == 0,
            )
        ).first()
        assert failed is not None, "expected a failed buy llm_run"
        assert failed.tool_result["reason"] == MSG_NO_MONEY
    finally:
        session.close()

    # ledger: 5 expenses + 1 income (wheat) + 1 work_wage
    txs = transaction_rows(eng, world_id, "agent_linxia")
    assert len(txs) == 7
    assert sum(1 for t in txs if t.type == "expense") == 5
    assert sum(1 for t in txs if t.type == "income") == 1
    assert sum(1 for t in txs if t.type == "work_wage") == 1
    assert txs[-1].balance_after == 23

    session = SessionLocal()
    try:
        employment = session.get(
            Employment,
            {"world_id": world_id, "agent_id": "agent_linxia", "job_id": "job_farm_field"},
        )
        assert employment is not None
        assert employment.hours_worked == pytest.approx(2.0)
        assert employment.total_earned == 30
    finally:
        session.close()

    completed = [
        e for e in eng.events_after(world_id, 0) if e.type == "work_completed"
    ]
    assert completed and completed[0].payload["wage"] == 30
    assert completed[0].payload["products"] == [{"item_id": "wheat", "quantity": 1}]

    # snapshot contract: inventory array + money visible
    snapshot = eng.snapshot(world_id)
    linxia = next(a for a in snapshot["agents"] if a["agent_id"] == "agent_linxia")
    assert linxia["money"] == 23
    assert linxia["inventory"] == [{"item_id": "bread", "quantity": 4}]

    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Hunger-response branch of the fake provider (multi-day robustness)
# --------------------------------------------------------------------------- #

def test_hungry_agent_eats_bread_first(world_config) -> None:
    eng = make_engine(world_config, wire_decisions=True)
    runtime = eng.create_world("饥饿世界", autonomous=True)
    world_id = runtime.world_id

    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        agent.hunger = 90
        # give her bread
        from app.database.models.inventories import Inventory
        session.add(Inventory(world_id=world_id, agent_id="agent_linxia", item_id="bread", quantity=2))
        session.commit()
    finally:
        session.close()

    advance_minutes(eng, world_id, 6)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id, LLMRun.agent_id == "agent_linxia")
        ).all()
        assert runs, "linxia made a decision"
        assert runs[-1].tool_name == "use_item"
        assert runs[-1].tool_arguments["item_id"] == "bread"
        assert runs[-1].success == 1
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent.hunger == 60  # bread restores 30
    finally:
        session.close()
    eng._runtimes.clear()


def test_hungry_agent_buys_bread_at_shop(world_config) -> None:
    eng = make_engine(world_config, wire_decisions=True)
    runtime = eng.create_world("饥饿世界2", autonomous=True)
    world_id = runtime.world_id

    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        agent.hunger = 95
        agent.col, agent.row = 23, 12  # park her at the shop
        agent.location_id = "village_shop"
        session.commit()
    finally:
        session.close()

    advance_minutes(eng, world_id, 6)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id, LLMRun.agent_id == "agent_linxia")
        ).all()
        assert runs[-1].tool_name == "buy_item"
        assert runs[-1].tool_arguments["item_id"] == "bread"
        assert runs[-1].success == 1
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent.money == 50 - 12
    finally:
        session.close()
    eng._runtimes.clear()
