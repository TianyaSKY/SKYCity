"""M19 tests: self-employed gathering jobs (伐木/钓鱼/采蜜) and per-job tool
bonuses (tool_axe / tool_rod / tool_sickle via work_bonus_jobs).

Drives the WorldEngine directly (no HTTP): clock advanced via tick +
engine._tick_runtime, exactly like test_economy.py / test_entrepreneurship.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.economy_service import EconomyService
from app.services.shop_service import ShopService
from app.services.stock_service import StockService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_world_engine import advance_minutes, agent_row

FOREST_ANCHOR = (20, 5)
RIVER_ANCHOR = (8, 32)
GARDEN_ANCHOR = (6, 14)


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


def make_engine(world_config: ParsedWorldConfig) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.economy_service = EconomyService(eng, SessionLocal)
    eng.stock_service = StockService(eng, SessionLocal)
    eng.shop_service = ShopService(eng, SessionLocal)
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


def work_completed(engine: WorldEngine, world_id: str) -> list:
    return [e for e in engine.events_after(world_id, 0) if e.type == "work_completed"]


# --------------------------------------------------------------------------- #
# Gathering jobs: woodcutting / fishing / honey
# --------------------------------------------------------------------------- #


def test_woodcutting_yields_wood_and_wage(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "forest", *FOREST_ANCHOR)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_woodcutting", reason="去树林砍柴"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 181)  # 480 -> 661: completes at 660

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 3012  # 3000 + wage 12
    completed = work_completed(engine, world_id)
    assert completed
    assert completed[0].payload["products"] == [{"item_id": "wood", "quantity": 3}]
    assert inventory_of(engine, world_id, "agent_linxia") == {"wood": 3}


def test_fishing_yields_fish_and_wage(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_zhangming", "river_bank", *RIVER_ANCHOR)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_zhangming", "job_fishing", reason="去河边钓鱼"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 181)

    row = agent_row(engine, world_id, "agent_zhangming")
    assert row.money == 3012  # 3000 + wage 12
    completed = work_completed(engine, world_id)
    assert completed
    assert completed[0].payload["products"] == [{"item_id": "fish", "quantity": 2}]
    assert inventory_of(engine, world_id, "agent_zhangming") == {"fish": 2}


def test_honey_collect_yields_honey(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_chenyu", "flower_garden", *GARDEN_ANCHOR)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_chenyu", "job_honey_collect", reason="去花圃收蜂蜜"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 121)  # completes at 600

    row = agent_row(engine, world_id, "agent_chenyu")
    assert row.money == 3010  # 3000 + wage 10
    completed = work_completed(engine, world_id)
    assert completed
    assert completed[0].payload["products"] == [{"item_id": "honey", "quantity": 1}]


def test_gathered_products_sell_to_shop(engine: WorldEngine) -> None:
    """The store buys wood/fish/honey, so gathering is a real income path."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", 23, 12)
    add_inventory(engine, world_id, "agent_linxia", "wood", 3)
    add_inventory(engine, world_id, "agent_linxia", "fish", 2)

    ok, envelope, reason = engine.economy_service.sell(
        world_id, "agent_linxia", "wood", quantity=3, reason="卖木材"
    )
    assert ok is True and reason is None
    assert envelope.payload["total"] == 18  # 3 × 6

    ok, envelope, reason = engine.economy_service.sell(
        world_id, "agent_linxia", "fish", quantity=2, reason="卖鲜鱼"
    )
    assert ok is True and reason is None
    assert envelope.payload["total"] == 18  # 2 × 9 (M19 buy price)

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.money == 3036  # 3000 + 36


# --------------------------------------------------------------------------- #
# Per-job tool bonuses (work_bonus_jobs beats flat work_bonus)
# --------------------------------------------------------------------------- #


def test_axe_boosts_woodcutting_only(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "forest", *FOREST_ANCHOR)
    add_inventory(engine, world_id, "agent_linxia", "tool_axe", 1)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_woodcutting", reason="拿斧头砍柴"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 181)

    completed = work_completed(engine, world_id)
    assert completed
    assert completed[0].payload["wage"] == 16  # 12 * 1.4 (axe +40%)


def test_axe_does_not_boost_unrelated_jobs(engine: WorldEngine) -> None:
    """The axe's work_bonus_jobs only lists job_woodcutting; the flat
    work_bonus is 0, so other jobs earn no bonus from it."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", 47, 24)
    add_inventory(engine, world_id, "agent_linxia", "tool_axe", 1)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="拿斧头干农活"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 121)

    completed = work_completed(engine, world_id)
    assert completed
    assert completed[0].payload["wage"] == 30  # no bonus: 30 * 1.0


def test_sickle_boosts_farm_field(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", 47, 24)
    add_inventory(engine, world_id, "agent_linxia", "tool_sickle", 1)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="拿镰刀干农活"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 121)

    completed = work_completed(engine, world_id)
    assert completed
    assert completed[0].payload["wage"] == 42  # 30 * 1.4 (sickle +40%)


def test_rod_boosts_fishing(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_zhangming", "river_bank", *RIVER_ANCHOR)
    add_inventory(engine, world_id, "agent_zhangming", "tool_rod", 1)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_zhangming", "job_fishing", reason="拿渔竿钓鱼"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 181)

    completed = work_completed(engine, world_id)
    assert completed
    assert completed[0].payload["wage"] == 16  # 12 * 1.4


def test_flat_work_bonus_still_applies(engine: WorldEngine) -> None:
    """Legacy tools (tool_rake, flat work_bonus=20) keep working everywhere."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_farm", 47, 24)
    add_inventory(engine, world_id, "agent_linxia", "tool_rake", 1)

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="拿耙子干农活"
    )
    assert ok is True and reason is None

    advance_minutes(engine, world_id, 121)

    completed = work_completed(engine, world_id)
    assert completed
    assert completed[0].payload["wage"] == 36  # 30 * 1.2
