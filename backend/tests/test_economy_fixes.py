"""A1/B1/C1/E2 fix tests: the economy repair batch.

A1  — upkeep flows into the village treasury and is recycled as universal UBI
      to all residents (debtors included — no poverty trap) plus wage
      subsidies to companies (money conserved, never destroyed).
B1  — the engine force-feeds idle hungry agents (inventory food first, then a
      shop buy), closing the consumption circuit without the LLM.
C1  — procurement shortfalls file pending orders; the engine's hourly tick
      fills them as soon as stock and funds exist.
E2  — zombie companies (consecutive losses + unpaid wages) are liquidated at
      the day boundary, and chronically absent employees lose their contract
      so the opening reopens.

Drives the WorldEngine directly (no HTTP, no background loop) exactly like
test_economy.py / test_stocks.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.companies import (
    Company,
    CompanyInventory,
    CompanyTransaction,
    EmploymentContract,
    JobOpening,
    ProcurementOrder,
    WorkShift,
)
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.stores import StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.world_events import WorldEvent
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.company_employment_service import CompanyEmploymentService
from app.services.economy_service import EconomyService
from app.services.stock_service import StockService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_company_employment import _next_shift
from tests.test_world_engine import advance_minutes, agent_row, place_agent, set_agent

SHOP_ANCHOR = (23, 12)
FARM_ANCHOR = (47, 24)


def _hire(service, world_id: str, company_id: str, applicant: str, manager: str) -> str:
    """Hire one worker at a company, return the employment id (mirrors the
    test_company_employment helper; that one unpacks a (engine, service)
    tuple fixture, this file wires the service directly)."""
    opening = next(
        o for o in service.list_openings(world_id)
        if o["company_id"] == company_id
    )
    application = service.apply(world_id, opening["opening_id"], applicant, "求职")
    reviewed = service.review(
        world_id, application["application_id"], manager, "accept", "录用"
    )
    return reviewed["employment_id"]


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
    eng.company_employment_service = CompanyEmploymentService(
        eng, SessionLocal, Path(get_settings().world_data_dir).resolve()
    )
    return eng


@pytest.fixture()
def engine(world_config: ParsedWorldConfig) -> WorldEngine:
    eng = make_engine(world_config)
    yield eng
    eng._runtimes.clear()


def agent_money(engine: WorldEngine, world_id: str, agent_id: str) -> int:
    return agent_row(engine, world_id, agent_id).money


def world_treasury(engine: WorldEngine, world_id: str) -> int:
    session = SessionLocal()
    try:
        return session.get(World, world_id).treasury
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# A1: treasury recycling
# --------------------------------------------------------------------------- #


def test_upkeep_lands_in_treasury_and_returns_as_ubi(engine: WorldEngine) -> None:
    """A1: 00:00 upkeep is collected (not destroyed); 50% returns as UBI."""
    runtime = engine.create_world()
    world_id = runtime.world_id

    advance_minutes(engine, world_id, 961)  # 480 -> 1441: crosses 00:00

    assert world_treasury(engine, world_id) >= 0
    session = SessionLocal()
    try:
        ubi = session.scalars(
            select(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.type == "ubi_income",
            )
        ).all()
        assert ubi, "UBI must be paid at the day boundary"
        assert all(t.amount == 60 for t in ubi)  # 1080 * 50% // 9
        assert all(t.reason == "村庄基本收入" for t in ubi)
        # The upkeep itself still hit the ledger (debt semantics preserved).
        upkeep = session.scalars(
            select(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.type == "upkeep",
            )
        ).all()
        assert len(upkeep) == 9
    finally:
        session.close()


def test_treasury_subsidizes_wage_paying_companies(engine: WorldEngine) -> None:
    """A1: the non-UBI half of the treasury subsidizes companies by the wages
    they paid that day — money circulates back into company accounts."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    service = engine.company_employment_service
    service.register_runtime(runtime)
    service.ensure_seeded(world_id)
    employment_id = _hire(service, world_id, "company_morning_farm", "agent_linxia", "agent_zhangming")
    shift = _next_shift(service, world_id, employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time)
    # The farm paid a 60 wage today; cross midnight -> it receives a subsidy.
    advance_minutes(engine, world_id, 1440 * 2 - runtime.clock.world_time)

    session = SessionLocal()
    try:
        subsidy = session.scalars(
            select(CompanyTransaction).where(
                CompanyTransaction.world_id == world_id,
                CompanyTransaction.type == "treasury_subsidy",
            )
        ).all()
        assert subsidy, "a wage-paying company must receive a treasury subsidy"
        farm = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert farm is not None and farm.money > 800 - 60  # 800 initial - wage + subsidy
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# B1: forced hunger eating
# --------------------------------------------------------------------------- #


def test_hungry_agent_force_eats_from_inventory(engine: WorldEngine) -> None:
    """B1: an idle agent with food at hand eats it without any LLM decision."""
    runtime = engine.create_world("饥饿世界", autonomous=True)
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", satiety=10, action_type=None)
    session = SessionLocal()
    try:
        session.add(
            Inventory(world_id=world_id, agent_id="agent_linxia", item_id="bread", quantity=2)
        )
        session.commit()
    finally:
        session.close()

    advance_minutes(engine, world_id, 61)  # crosses the next hour boundary

    row = agent_row(engine, world_id, "agent_linxia")
    assert row.satiety > 10  # 10 + 30 (bread) - hourly drain
    session = SessionLocal()
    try:
        bread = session.get(
            Inventory, {"world_id": world_id, "agent_id": "agent_linxia", "item_id": "bread"}
        )
        assert bread is not None and bread.quantity == 1  # one eaten
    finally:
        session.close()


def test_hungry_agent_force_buys_food_at_shop(engine: WorldEngine) -> None:
    """B1: hungry + at a shop + money -> forced cheapest-food purchase, then
    the food is eaten on the next hourly tick (satiety recovers)."""
    runtime = engine.create_world("饥饿买饭世界", autonomous=True)
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)
    set_agent(engine, world_id, "agent_linxia", satiety=5, money=200, action_type=None)

    advance_minutes(engine, world_id, 61)  # hour 1: buy food
    row = agent_row(engine, world_id, "agent_linxia")
    money_after_buy = row.money
    assert money_after_buy < 200  # paid for food
    session = SessionLocal()
    try:
        expense = session.scalars(
            select(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.agent_id == "agent_linxia",
                Transaction.type == "expense",
            )
        ).all()
        assert expense, "the forced buy must hit the agent ledger"
    finally:
        session.close()

    advance_minutes(engine, world_id, 61)  # hour 2: engine eats the food
    row = agent_row(engine, world_id, "agent_linxia")
    assert row.satiety > 5  # bought + eaten (satiety recovered)


# --------------------------------------------------------------------------- #
# C1: procurement orders
# --------------------------------------------------------------------------- #


def test_stock_shortage_files_order_and_hourly_tick_fills_it(
        engine: WorldEngine,
) -> None:
    """C1: 卖方无货 -> 订单；下一小时库存到位 -> 自动履约."""
    runtime = engine.create_world("订单测试")
    world_id = runtime.world_id
    service = engine.company_employment_service
    service.register_runtime(runtime)
    service.ensure_seeded(world_id)

    # Baker has no wheat stock: the purchase files an order instead of failing.
    payload = service.purchase_company_goods(
        world_id,
        "company_village_bakery",
        "company_morning_farm",
        "agent_touzi",
        "wheat",
        quantity=10,
        reason="备料",
    )
    assert payload.get("ordered") is True
    assert payload.get("order_id")
    session = SessionLocal()
    try:
        order = session.get(ProcurementOrder, payload["order_id"])
        assert order is not None and order.status == "open"
        assert order.quantity == 10 and order.unit_price == 6
    finally:
        session.close()

    # The farm produces wheat -> the next hourly tick fills the order.
    employment_id = _hire(service, world_id, "company_morning_farm", "agent_linxia", "agent_zhangming")
    shift = _next_shift(service, world_id, employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    place_agent(engine, world_id, "agent_linxia", "village_farm", *FARM_ANCHOR)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time + 61)

    session = SessionLocal()
    try:
        order = session.get(ProcurementOrder, payload["order_id"])
        assert order is not None and order.status == "filled"
        bakery = session.get(
            Company, {"world_id": world_id, "company_id": "company_village_bakery"}
        )
        assert bakery is not None and bakery.money == 300 - 60
        farm = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert farm is not None and farm.money == 800 - 60 + 60
        wheat = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_village_bakery", "item_id": "wheat"},
        )
        assert wheat is not None and wheat.quantity == 10
        events = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "procurement_order_filled",
            )
        ).all()
        assert len(events) == 1
    finally:
        session.close()


def test_open_orders_listed(engine: WorldEngine) -> None:
    runtime = engine.create_world("订单列表测试")
    world_id = runtime.world_id
    service = engine.company_employment_service
    service.register_runtime(runtime)
    service.ensure_seeded(world_id)
    service.purchase_company_goods(
        world_id, "company_village_bakery", "company_morning_farm",
        "agent_touzi", "wheat", quantity=5, reason="备料",
    )
    orders = service.list_open_orders(world_id)
    assert len(orders) == 1
    assert orders[0]["quantity"] == 5
    assert orders[0]["status"] == "open"


# --------------------------------------------------------------------------- #
# E2: zombie liquidation + absence termination
# --------------------------------------------------------------------------- #


def test_zombie_company_liquidated_at_day_boundary(engine: WorldEngine) -> None:
    """E2: 连续亏损 + 欠薪的企业在日界被清算（关闭、解约、停招）."""
    runtime = engine.create_world("清算测试")
    world_id = runtime.world_id
    service = engine.company_employment_service
    service.register_runtime(runtime)
    service.ensure_seeded(world_id)
    employment_id = _hire(service, world_id, "company_morning_farm", "agent_linxia", "agent_zhangming")
    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        company.money = 0
        company.consecutive_loss_days = 3  # already past ZOMBIE_LOSS_DAYS
        company.unpaid_wage_total = 120
        contract = session.get(EmploymentContract, employment_id)
        contract.unpaid_wage = 120
        session.commit()
    finally:
        session.close()

    advance_minutes(engine, world_id, 1440 - runtime.clock.world_time + 1)

    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert company is not None and company.status == "closed"
        assert company.closed_at is not None
        contract = session.get(EmploymentContract, employment_id)
        assert contract is not None and contract.status == "terminated"
        opening = session.scalar(
            select(JobOpening).where(
                JobOpening.world_id == world_id,
                JobOpening.position_id == "position_farm_worker",
            )
        )
        assert opening is not None and opening.status == "closed"
    finally:
        session.close()


def test_absent_employee_contract_terminated_and_opening_reopens(
        engine: WorldEngine,
) -> None:
    """E2: 连续缺勤 MAX_ABSENT_SHIFTS 次 → 自动解约 + 招聘重开."""
    runtime = engine.create_world("缺勤解约测试")
    world_id = runtime.world_id
    service = engine.company_employment_service
    service.register_runtime(runtime)
    service.ensure_seeded(world_id)
    employment_id = _hire(service, world_id, "company_morning_farm", "agent_linxia", "agent_zhangming")
    session = SessionLocal()
    try:
        opening = session.scalar(
            select(JobOpening).where(
                JobOpening.world_id == world_id,
                JobOpening.position_id == "position_farm_worker",
            )
        )
        vacancies_before = opening.vacancies  # 2 - 1 hire = 1
        contract = session.get(EmploymentContract, employment_id)
        contract.absent_shifts = 2  # one more absence crosses the threshold
        session.commit()
    finally:
        session.close()
    # Force the next scheduled shift to be marked absent (nobody shows up).
    view = service.list_agent_employment(world_id, "agent_linxia")
    shift = next(s for s in view["shifts"] if s["status"] == "scheduled")
    advance_minutes(engine, world_id, shift["scheduled_start"] + 121 - runtime.clock.world_time)

    session = SessionLocal()
    try:
        contract = session.get(EmploymentContract, employment_id)
        assert contract is not None and contract.status == "terminated"
        assert contract.termination_reason and "缺勤" in contract.termination_reason
        opening = session.scalar(
            select(JobOpening).where(
                JobOpening.world_id == world_id,
                JobOpening.position_id == "position_farm_worker",
            )
        )
        assert opening is not None and opening.status == "open"
        assert opening.vacancies == vacancies_before + 1  # slot released
    finally:
        session.close()
