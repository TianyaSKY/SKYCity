"""M12 tests: consumption activation.

Covers the six levers: mood dimension (D7 — hourly drain/sleep/wait restore,
mood items usable, low-mood decision boost, snapshot + save/restore
roundtrip), productive item effects (C4 — tool wage bonus, fertilizer yield
bonus), promo pricing (D5 — deterministic hash roll + restock application),
daily upkeep (D6) and gift -> relationship (B3).

Drives the WorldEngine directly (no HTTP, no background loop), exactly like
test_economy.py / test_transfers.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.relationships import Relationship
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import StoreProduct
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.economy_service import MSG_NOT_FOOD, EconomyService
from app.services.god_action_service import GodActionService
from app.services.save_service import SaveService
from app.services.transfer_service import TransferService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine, _promo_roll

from tests.test_economy import (
    add_inventory,
    inventory_of,
    place_agent,
    set_agent,
    set_stock,
    transaction_rows,
)
from tests.test_world_engine import advance_minutes, agent_row

SHOP_ANCHOR = (23, 12)
FARM_ANCHOR = (47, 24)
PLAZA_ANCHOR = (32, 20)


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
    eng.transfer_service = TransferService(eng, SessionLocal)
    eng.god_action_service = GodActionService(eng, SessionLocal)
    eng.save_service = SaveService(eng, SessionLocal)
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


def pending_decides(engine: WorldEngine, world_id: str, agent_id: str):
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.world_id == world_id,
                    ScheduledAction.agent_id == agent_id,
                    ScheduledAction.action_type == "agent_decide",
                )
            ).all()
        )
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# D7 mood dimension
# --------------------------------------------------------------------------- #


def test_mood_drains_hourly_and_sleep_restores(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id

    advance_minutes(engine, world_id, 60)  # 480 -> 540: one hour boundary

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.mood == 99  # base drain -1/h

    envelopes = engine.events_after(world_id, 0)
    needs = [
        e
        for e in envelopes
        if e.type == "needs_changed" and e.payload["agent_id"] == "agent_linxia"
    ]
    assert needs, "needs_changed must be emitted on the hourly tick"
    assert needs[-1].payload["mood"] == 99

    set_agent(engine, world_id, "agent_linxia", action_type="sleep")
    advance_minutes(engine, world_id, 60)  # 540 -> 600

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.mood == 100  # -1 drain then +10 sleep recovery, capped


def test_mood_low_boosts_decision(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config, wire_decisions=True)
    runtime = eng.create_world("心情世界", autonomous=True)
    world_id = runtime.world_id
    # Drop the autonomous initial decision so linxia stays idle until the
    # hourly boost window (otherwise the scripted provider would be mid-action
    # and the boost guard ``action_type is None`` would not hold).
    session = SessionLocal()
    try:
        eng.get_runtime(world_id).scheduler.cancel_for_agent(session, "agent_linxia")
        session.commit()
    finally:
        session.close()
    set_agent(eng, world_id, "agent_linxia", mood=20)

    advance_minutes(eng, world_id, 60)  # 480 -> 540; tick at 540 drains mood to 19

    decides = pending_decides(eng, world_id, "agent_linxia")
    assert decides, "low mood must schedule an agent_decide"
    assert decides[0].due_at == 541
    assert decides[0].payload == {"origin": "needs_boost"}
    eng._runtimes.clear()


def test_mood_item_usable_and_restores(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", hunger=40, mood=50)
    add_inventory(engine, world_id, "agent_linxia", "candle", 1)

    ok, envelope, reason = engine.economy_service.use_item(
        world_id, "agent_linxia", "candle", reason="心情不好"
    )
    assert ok is True and reason is None
    assert envelope.type == "item_used"
    assert envelope.payload["hunger_before"] == 40
    assert envelope.payload["hunger_after"] == 40  # candle does not feed
    assert envelope.payload["mood_before"] == 50
    assert envelope.payload["mood_after"] == 58  # +8 mood_restore

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.hunger == 40
    assert row.mood == 58
    assert inventory_of(engine, world_id, "agent_linxia") == {}


def test_non_effect_item_still_rejected(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    add_inventory(engine, world_id, "agent_linxia", "wheat", 1)

    ok, envelope, reason = engine.economy_service.use_item(
        world_id, "agent_linxia", "wheat", reason="试试"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_FOOD


# --------------------------------------------------------------------------- #
# C4 productive item effects
# --------------------------------------------------------------------------- #


def test_tool_rake_boosts_wage(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)
    add_inventory(engine, world_id, "agent_linxia", "tool_rake", 1)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干农活"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 121)  # 480 -> 601: completes at 600

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 86  # 50 + wage 30 * 1.2 = 36
    txs = transaction_rows(engine, world_id, "agent_linxia")
    assert txs[-1].type == "work_wage"
    assert txs[-1].amount == 36
    completed = [
        e for e in engine.events_after(world_id, 0) if e.type == "work_completed"
    ]
    assert completed
    assert completed[0].payload["wage"] == 36


def test_fertilizer_boosts_yield(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)
    add_inventory(engine, world_id, "agent_linxia", "fertilizer", 1)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="施肥干活"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 121)

    completed = [
        e for e in engine.events_after(world_id, 0) if e.type == "work_completed"
    ]
    assert completed
    assert completed[0].payload["products"] == [{"item_id": "wheat", "quantity": 2}]
    # the fertilizer input itself stays in the backpack
    assert inventory_of(engine, world_id, "agent_linxia") == {"fertilizer": 1, "wheat": 2}


# --------------------------------------------------------------------------- #
# D6 daily upkeep
# --------------------------------------------------------------------------- #


def test_daily_upkeep_deducted(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id

    advance_minutes(engine, world_id, 961)  # 480 -> 1441: crosses 00:00

    for agent_id in ("agent_linxia", "agent_zhangming"):
        row = agent_row(engine, world_id, agent_id)
        assert row.money == 45  # 50 - 5 upkeep
    row = agent_row(engine, world_id, "agent_touzi")
    assert row.money == 495  # 500 - 5 upkeep (identity initial_money)

    txs = transaction_rows(engine, world_id, "agent_linxia")
    upkeep = [t for t in txs if t.type == "upkeep"]
    assert len(upkeep) == 1
    assert upkeep[0].amount == -5
    assert upkeep[0].reason == "每日生活开销"
    assert upkeep[0].balance_after == 45

    events = engine.events_after(world_id, 0)
    moved = [
        e
        for e in events
        if e.type == "money_changed"
        and e.payload["agent_id"] == "agent_linxia"
        and e.payload.get("reason") == "每日生活开销"
    ]
    assert moved and moved[-1].payload == {
        "agent_id": "agent_linxia",
        "amount": -5,
        "balance": 45,
        "reason": "每日生活开销",
    }


# --------------------------------------------------------------------------- #
# D5 promo pricing
# --------------------------------------------------------------------------- #


def test_promo_roll_deterministic_and_distributed() -> None:
    world_id, store_id, item_id = "world_x", "village_shop", "bread"
    hits = 0
    trials = 400
    for day in range(1, trials + 1):
        assert _promo_roll(world_id, store_id, item_id, day) == _promo_roll(
            world_id, store_id, item_id, day
        )
        if _promo_roll(world_id, store_id, item_id, day):
            hits += 1
    rate = hits / trials
    assert 0.1 < rate < 0.3, f"promo rate {rate} should sit near 20%"


def test_restock_applies_promo_prices(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_stock(engine, world_id, "bread", 0)

    advance_minutes(engine, world_id, 1440)  # 480 -> day2 08:00 (1920), open hour

    day = 1920 // 1440
    session = SessionLocal()
    try:
        products = session.scalars(
            select(StoreProduct).where(
                StoreProduct.world_id == world_id,
                StoreProduct.store_id == "village_shop",
            )
        ).all()
        assert products
        for product in products:
            promo = _promo_roll(world_id, "village_shop", product.item_id, day)
            expected = (
                max(1, round(product.base_sell_price * 0.8))
                if promo
                else product.base_sell_price
            )
            assert product.sell_price == expected, (
                f"{product.item_id}: expected {expected}, got {product.sell_price}"
            )
    finally:
        session.close()

    events = engine.events_after(world_id, 0)
    price_events = [e for e in events if e.type == "store_price_changed"]
    session = SessionLocal()
    try:
        products = session.scalars(
            select(StoreProduct).where(
                StoreProduct.world_id == world_id,
                StoreProduct.store_id == "village_shop",
            )
        ).all()
        promo_ids = {
            p.item_id
            for p in products
            if _promo_roll(world_id, "village_shop", p.item_id, day)
        }
    finally:
        session.close()
    assert {e.payload["item_id"] for e in price_events} == promo_ids
    assert all(e.payload["promo"] is True for e in price_events)
    # deterministic roll: honey/tool_rake/candle are promo on day 1 for
    # world_001 — the price events must carry them.
    assert promo_ids >= {"honey", "tool_rake", "candle"}


# --------------------------------------------------------------------------- #
# B3 gift -> relationship
# --------------------------------------------------------------------------- #


def test_gift_improves_relationship(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_plaza", *PLAZA_ANCHOR)
    place_agent(engine, world_id, "agent_zhangming", "village_plaza", *PLAZA_ANCHOR)
    add_inventory(engine, world_id, "agent_linxia", "bread", 1)

    ok, envelope, reason = engine.transfer_service.give_item(
        world_id, "agent_linxia", "agent_zhangming", "bread", quantity=1, reason="送你面包"
    )
    assert ok is True and reason is None
    assert envelope.type == "item_given"

    session = SessionLocal()
    try:
        linxia_to_zhang = session.get(
            Relationship,
            {"world_id": world_id, "source_agent_id": "agent_linxia",
             "target_agent_id": "agent_zhangming"},
        )
        assert linxia_to_zhang is not None
        assert (linxia_to_zhang.affection, linxia_to_zhang.familiarity) == (3, 2)
        zhang_to_linxia = session.get(
            Relationship,
            {"world_id": world_id, "source_agent_id": "agent_zhangming",
             "target_agent_id": "agent_linxia"},
        )
        assert zhang_to_linxia is not None
        assert (zhang_to_linxia.affection, zhang_to_linxia.familiarity) == (0, 2)
    finally:
        session.close()

    changed = [
        e
        for e in engine.events_after(world_id, 0)
        if e.type == "relationship_changed"
    ]
    assert changed, "gift must emit relationship_changed deltas"
    by_source = {e.payload["source_agent_id"]: e.payload for e in changed}
    assert by_source["agent_linxia"]["deltas"] == {"affection": 3, "familiarity": 2}
    assert by_source["agent_zhangming"]["deltas"] == {"familiarity": 2}


# --------------------------------------------------------------------------- #
# D7 mood survives save/restore (M12: no schema bump, _row_dict serializes all)
# --------------------------------------------------------------------------- #


def test_mood_survives_save_restore(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("心情存档")
    world_id = runtime.world_id
    set_agent(eng, world_id, "agent_linxia", mood=37)

    result = eng.save_service.save(world_id)
    restored = eng.save_service.restore(result.save_id)
    new_id = restored.world_id

    row = agent_row(eng, new_id, "agent_linxia")
    assert row.mood == 37
    eng._runtimes.clear()
