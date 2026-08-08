"""M10 tests: the stock market — seeding, trading gates (R18.1), business +
hourly price moves (R18.2), daily dividends (R18.3), god overrides (R18.4),
save/restore (R18.5), the HTTP contract and the LLM buy_stock tool.

Drives the WorldEngine directly (no HTTP, no background loop) exactly like
test_economy.py, plus one TestClient block for the REST contracts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.saves import Save
from app.database.models.stocks import Stock, StockHolding
from app.database.session import SessionLocal
from app.main import app
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.economy_service import EconomyService
from app.services.god_action_service import GodActionService
from app.services.save_service import SaveService
from app.services.stock_service import (
    MSG_NOT_ENOUGH_SHARES,
    MSG_STOCK_MISSING,
    StockService,
    _hourly_noise,
)
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_economy import place_agent, set_agent, transaction_rows
from tests.test_world_engine import advance_minutes

SHOP_ANCHOR = (23, 12)

STOCK_SHOP = "stock_village_shop"
STOCK_FARM = "stock_village_farm"
STOCK_DELIVERY = "stock_delivery"

BASE_PRICES = {
    STOCK_SHOP: 20,
    STOCK_FARM: 15,
    STOCK_DELIVERY: 12,
}


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


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
    eng.stock_service = StockService(eng, SessionLocal)
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


def stock_row(engine: WorldEngine, world_id: str, stock_id: str) -> Stock:
    session = SessionLocal()
    try:
        return session.get(Stock, {"world_id": world_id, "stock_id": stock_id})
    finally:
        session.close()


def holding_of(
        engine: WorldEngine, world_id: str, agent_id: str, stock_id: str
) -> int:
    session = SessionLocal()
    try:
        row = session.get(
            StockHolding,
            {"world_id": world_id, "agent_id": agent_id, "stock_id": stock_id},
        )
        return row.shares if row is not None else 0
    finally:
        session.close()


def holding_avg_cost(
        engine: WorldEngine, world_id: str, agent_id: str, stock_id: str
) -> int:
    session = SessionLocal()
    try:
        row = session.get(
            StockHolding,
            {"world_id": world_id, "agent_id": agent_id, "stock_id": stock_id},
        )
        return row.avg_cost if row is not None else 0
    finally:
        session.close()


def set_stock_price(engine: WorldEngine, world_id: str, stock_id: str, price: int) -> None:
    session = SessionLocal()
    try:
        session.execute(
            update(Stock)
            .where(Stock.world_id == world_id, Stock.stock_id == stock_id)
            .values(price=price)
        )
        session.commit()
    finally:
        session.close()


def set_day_business(engine: WorldEngine, world_id: str, stock_id: str, count: int) -> None:
    session = SessionLocal()
    try:
        session.execute(
            update(Stock)
            .where(Stock.world_id == world_id, Stock.stock_id == stock_id)
            .values(day_business=count)
        )
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Seeding (R18 base prices far below the 50-coin starting wallet)
# --------------------------------------------------------------------------- #


def test_seed_stocks_at_base_price(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    data = engine.stock_service.list_stocks(runtime.world_id)
    assert data is not None
    assert [s["stock_id"] for s in data["stocks"]] == [STOCK_DELIVERY, STOCK_FARM, STOCK_SHOP]
    for stock in data["stocks"]:
        assert stock["price"] == BASE_PRICES[stock["stock_id"]]
        assert stock["prev_price"] == BASE_PRICES[stock["stock_id"]]
        assert stock["day_business"] == 0
        assert stock["last_div_per_share"] == 0
    assert data["holdings"] == []


def test_investor_agent_spawns_with_500(engine: WorldEngine) -> None:
    """M10 investor resident: 钱多多 spawns at the plaza with 500 coins."""
    runtime = engine.create_world()
    session = SessionLocal()
    try:
        row = session.get(Agent, {"world_id": runtime.world_id, "agent_id": "agent_touzi"})
        assert row is not None, "agent_touzi must be seeded from the map spawn"
        assert row.money == 5000  # identity initial_money, not the 3000 default
        assert (row.col, row.row) == (33, 20)  # plaza spawn cell
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Trading gates (R18.1)
# --------------------------------------------------------------------------- #


def test_buy_stock_success(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="投资"
    )
    assert ok is True and reason is None
    assert envelope.type == "stock_bought"
    assert envelope.payload == {
        "agent_id": "agent_linxia",
        "stock_id": STOCK_SHOP,
        "stock_name": "晨露商店",
        "shares": 2,
        "unit_price": 20,
        "total": 40,
    }
    assert agent_row_money(engine, world_id, "agent_linxia") == 2960  # 3000 - 2*20
    assert holding_of(engine, world_id, "agent_linxia", STOCK_SHOP) == 2

    types = [e.type for e in engine.events_after(world_id, 0)]
    assert "stock_bought" in types
    assert "money_changed" in types
    txs = transaction_rows(engine, world_id, "agent_linxia")
    assert len(txs) == 1
    assert txs[0].type == "stock_buy"
    assert txs[0].amount == -40
    assert txs[0].item_id == STOCK_SHOP
    assert txs[0].quantity == 2


def test_holding_avg_cost_weighted_average(engine: WorldEngine) -> None:
    """avg_cost tracks the weighted buy price; sells never change it."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", money=1000)
    assert engine.stock_service.buy_stock(world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="买")[0]
    assert holding_avg_cost(engine, world_id, "agent_linxia", STOCK_SHOP) == 20  # 首笔按市价

    set_stock_price(engine, world_id, STOCK_SHOP, 22)
    assert engine.stock_service.buy_stock(world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="买")[0]
    assert holding_of(engine, world_id, "agent_linxia", STOCK_SHOP) == 4
    assert holding_avg_cost(engine, world_id, "agent_linxia", STOCK_SHOP) == 21  # (40+44)/4

    assert engine.stock_service.sell_stock(world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="用钱")[0]
    assert holding_of(engine, world_id, "agent_linxia", STOCK_SHOP) == 3
    assert holding_avg_cost(engine, world_id, "agent_linxia", STOCK_SHOP) == 21  # 卖出不改成本


def test_buy_insufficient_funds(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", money=5)

    ok, envelope, reason = engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="投资"
    )
    assert ok is False and envelope is None
    assert reason == "余额不足"
    assert agent_row_money(engine, world_id, "agent_linxia") == 5
    assert holding_of(engine, world_id, "agent_linxia", STOCK_SHOP) == 0
    types = [e.type for e in engine.events_after(world_id, 0)]
    assert "stock_bought" not in types


def test_buy_busy_rejected(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", action_type="work")

    ok, envelope, reason = engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="投资"
    )
    assert ok is False and envelope is None
    assert reason == "当前行动未完成"  # R1: trading requires idle


def test_buy_unknown_stock(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.stock_service.buy_stock(
        runtime.world_id, "agent_linxia", "nope", shares=1, reason="投资"
    )
    assert ok is False and envelope is None
    assert reason == MSG_STOCK_MISSING


def test_sell_stock_success(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", money=100)
    assert engine.stock_service.buy_stock(world_id, "agent_linxia", STOCK_SHOP, shares=3, reason="买")[0]
    assert agent_row_money(engine, world_id, "agent_linxia") == 40

    ok, envelope, reason = engine.stock_service.sell_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="用钱"
    )
    assert ok is True and reason is None
    assert envelope.type == "stock_sold"
    assert envelope.payload == {
        "agent_id": "agent_linxia",
        "stock_id": STOCK_SHOP,
        "stock_name": "晨露商店",
        "shares": 2,
        "unit_price": 20,
        "total": 40,
    }
    assert agent_row_money(engine, world_id, "agent_linxia") == 80  # 40 + 2*20
    assert holding_of(engine, world_id, "agent_linxia", STOCK_SHOP) == 1

    types = [e.type for e in engine.events_after(world_id, 0)]
    assert "stock_sold" in types
    assert "money_changed" in types
    txs = transaction_rows(engine, world_id, "agent_linxia")
    assert [t.type for t in txs] == ["stock_buy", "stock_sell"]
    assert txs[-1].amount == 40


def test_sell_more_than_held(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", money=100)
    assert engine.stock_service.buy_stock(world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="买")[0]

    ok, envelope, reason = engine.stock_service.sell_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="卖"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_ENOUGH_SHARES
    assert holding_of(engine, world_id, "agent_linxia", STOCK_SHOP) == 1


def test_sell_without_holding(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, envelope, reason = engine.stock_service.sell_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="卖"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_ENOUGH_SHARES


# --------------------------------------------------------------------------- #
# Price mechanics (R18.2): business +1, hourly noise, hourly quote events
# --------------------------------------------------------------------------- #


def test_price_moves_on_business_and_hourly_noise(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)

    for _ in range(2):
        ok, _, reason = engine.economy_service.buy(
            world_id, "agent_linxia", "bread", quantity=1, reason="买面包"
        )
        assert ok is True, reason
    row = stock_row(engine, world_id, STOCK_SHOP)
    assert row.day_business == 2
    assert row.price == 22  # base 20 + 2 business bumps

    advance_minutes(engine, world_id, 61)  # 480 -> 541: crosses hour 9 (540)

    expected = max(1, 22 + _hourly_noise(world_id, STOCK_SHOP, 9))
    row = stock_row(engine, world_id, STOCK_SHOP)
    assert row.price == expected
    quotes = [
        e for e in engine.events_after(world_id, 0) if e.type == "stock_price_changed"
    ]
    assert quotes, "hourly stock_price_changed must be published"
    assert quotes[-1].payload["stock_id"] == STOCK_SHOP


# --------------------------------------------------------------------------- #
# Daily dividends (R18.3)
# --------------------------------------------------------------------------- #


def test_daily_dividend_paid(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, _, reason = engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="投资"
    )
    assert ok is True, reason
    set_day_business(engine, world_id, STOCK_SHOP, 12)

    advance_minutes(engine, world_id, 961)  # 480 -> 1441: crosses 00:00 of day 2

    div_events = [
        e for e in engine.events_after(world_id, 0) if e.type == "dividend_paid"
    ]
    assert div_events, "dividend_paid must be published at the day boundary"
    assert div_events[0].payload == {
        "stock_id": STOCK_SHOP,
        "stock_name": "晨露商店",
        "div_per_share": 4,  # max(1, 12 // 3) — M19 门槛 5→3
        "payouts": [{"agent_id": "agent_linxia", "shares": 2, "amount": 8}],
    }
    row = stock_row(engine, world_id, STOCK_SHOP)
    assert row.day_business == 0
    assert row.last_div_per_share == 4
    assert row.prev_price == row.price  # close price snapshot

    assert agent_row_money(engine, world_id, "agent_linxia") == 2848  # 3000-40, +8 dividend, -120 M12 daily upkeep
    txs = transaction_rows(engine, world_id, "agent_linxia")
    dividend_tx = [t for t in txs if t.type == "dividend"][-1]
    assert dividend_tx.amount == 8
    assert dividend_tx.item_id == STOCK_SHOP
    assert dividend_tx.quantity == 2
    upkeep_tx = [t for t in txs if t.type == "upkeep"][-1]
    assert upkeep_tx.amount == -120

    money = [
        e
        for e in engine.events_after(world_id, 0)
        if e.type == "money_changed"
           and e.payload["agent_id"] == "agent_linxia"
           and e.payload["amount"] == 8
    ]
    assert money, "dividend payout must surface as money_changed"


# --------------------------------------------------------------------------- #
# God override (R18.4)
# --------------------------------------------------------------------------- #


def test_god_change_stock_price(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    result = engine.god_action_service.apply(
        world_id,
        "change_stock_price",
        parameters={"stock_id": STOCK_SHOP, "price": 30},
        reason="测试",
    )
    assert result["success"] is True
    assert result["result"] == {"stock_id": STOCK_SHOP, "price": 30}
    types = [e["type"] for e in result["events"]]
    assert types == ["god_action_applied", "stock_price_changed"]
    changed = result["events"][1]["payload"]
    assert changed["price"] == 30
    assert changed["stock_name"] == "晨露商店"
    assert stock_row(engine, world_id, STOCK_SHOP).price == 30

    with pytest.raises(HTTPException) as exc:
        engine.god_action_service.apply(
            world_id,
            "change_stock_price",
            parameters={"stock_id": STOCK_SHOP, "price": 0},
            reason="测试",
        )
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------- #
# Save / restore (R18.5)
# --------------------------------------------------------------------------- #


def test_save_restore_preserves_stocks(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    ok, _, reason = engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="投资"
    )
    assert ok is True, reason
    saved = engine.save_service.save(world_id)
    assert saved is not None

    restored = engine.save_service.restore(saved.save_id)
    new_world_id = restored.world_id
    data = engine.stock_service.list_stocks(new_world_id)
    assert data is not None
    shop = next(s for s in data["stocks"] if s["stock_id"] == STOCK_SHOP)
    assert shop["price"] == stock_row(engine, world_id, STOCK_SHOP).price
    assert shop["day_business"] == stock_row(engine, world_id, STOCK_SHOP).day_business
    assert any(
        h["agent_id"] == "agent_linxia"
        and h["stock_id"] == STOCK_SHOP
        and h["shares"] == 2
        and h["avg_cost"] == 20  # 买入价即成本，随存档恢复
        for h in data["holdings"]
    )

    # the restored world's event stream continues the saved max_sequence
    max_seq = saved_sequence(engine, saved.save_id)
    tail = engine.events_after(new_world_id, max_seq)
    assert tail, "events after restore must continue the saved sequence"


def test_restore_old_save_without_stocks(engine: WorldEngine) -> None:
    """Pre-M10 saves carry no market; restore seeds it (R18.5 fallback)."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    saved = engine.save_service.save(world_id)

    session = SessionLocal()
    try:
        row = session.get(Save, saved.save_id)
        payload = dict(row.payload_json)
        payload.pop("stocks", None)
        payload.pop("stock_holdings", None)
        row.payload_json = payload
        session.commit()
    finally:
        session.close()

    restored = engine.save_service.restore(saved.save_id)
    data = engine.stock_service.list_stocks(restored.world_id)
    assert data is not None
    assert [s["stock_id"] for s in data["stocks"]] == [STOCK_DELIVERY, STOCK_FARM, STOCK_SHOP]
    for stock in data["stocks"]:
        assert stock["price"] == BASE_PRICES[stock["stock_id"]]
    assert data["holdings"] == []


# --------------------------------------------------------------------------- #
# HTTP contract (TestClient)
# --------------------------------------------------------------------------- #


def test_http_action_contract(client: TestClient) -> None:
    created = client.post("/api/worlds", json={"name": "股市API", "autonomous": False})
    assert created.status_code == 201, created.text
    world_id = created.json()["world_id"]

    stocks = client.get(f"/api/worlds/{world_id}/stocks")
    assert stocks.status_code == 200
    body = stocks.json()
    assert [s["stock_id"] for s in body["stocks"]] == [STOCK_DELIVERY, STOCK_FARM, STOCK_SHOP]
    assert body["stocks"][0]["price"] == 12
    assert body["holdings"] == []

    # Pin the balance down so the second buy overdraws (initial money is
    # 3000 since M19 — the old 50-default scenario no longer holds).
    pin = client.post(
        f"/api/worlds/{world_id}/god-actions",
        json={
            "command_type": "deduct_money",
            "target_id": "agent_linxia",
            "parameters": {"amount": 2950},
            "reason": "test",
        },
    )
    assert pin.status_code == 200, pin.text

    bought = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={
            "action_type": "buy_stock",
            "stock_id": STOCK_SHOP,
            "shares": 2,
            "reason": "test",
        },
    )
    assert bought.status_code == 200, bought.text
    assert bought.json()["event"]["type"] == "stock_bought"

    short = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={
            "action_type": "buy_stock",
            "stock_id": STOCK_SHOP,
            "shares": 3,
            "reason": "test",
        },
    )
    assert short.status_code == 409
    assert short.json()["reason"] == "余额不足"

    stocks = client.get(f"/api/worlds/{world_id}/stocks")
    holdings = stocks.json()["holdings"]
    assert holdings == [
        {
            "agent_id": "agent_linxia",
            "stock_id": STOCK_SHOP,
            "shares": 2,
            "avg_cost": 20,  # 首笔按市价记成本
        }
    ]


# --------------------------------------------------------------------------- #
# LLM scripted decision (buy_stock tool through the decision service)
# --------------------------------------------------------------------------- #


def test_llm_script_buy_stock(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(
        world_config,
        scripts={
            "agent_linxia": [
                ("buy_stock", {"stock_id": STOCK_SHOP, "shares": 2, "reason": "投资"})
            ]
        },
        wire_decisions=True,
    )
    runtime = eng.create_world("股市决策", autonomous=True)
    world_id = runtime.world_id

    done = False
    for _ in range(3):
        advance_minutes(eng, world_id, 10)
        if holding_of(eng, world_id, "agent_linxia", STOCK_SHOP) == 2:
            done = True
            break
    assert done, "scripted buy_stock decision did not execute"
    assert agent_row_money(eng, world_id, "agent_linxia") == 2960  # 3000 - 2*20
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def agent_row_money(engine: WorldEngine, world_id: str, agent_id: str) -> int:
    session = SessionLocal()
    try:
        row = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert row is not None
        return row.money
    finally:
        session.close()


def saved_sequence(engine: WorldEngine, save_id: str) -> int:
    session = SessionLocal()
    try:
        row = session.get(Save, save_id)
        return int((row.payload_json or {}).get("max_sequence") or 0)
    finally:
        session.close()
