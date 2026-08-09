"""Cooperative-share tests: seeded issuance, fixed-price subscriptions and
redemptions, transparent operating volume, administrator price changes,
persistence, HTTP, and the decision tool.

Drives ``WorldEngine`` directly except for the HTTP contract test.  The stable
``stock_*`` IDs and ``buy_stock`` / ``sell_stock`` action IDs are API contracts;
all resident-facing behavior is cooperative-share behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.companies import Company, CompanyTransaction
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
    MSG_HOLDING_CAP,
    MSG_ISSUANCE_EXHAUSTED,
    MSG_NOT_ENOUGH_SHARES,
    MSG_STOCK_MISSING,
    StockService,
)
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_economy import place_agent, set_agent, transaction_rows
from tests.test_world_engine import advance_minutes

SHOP_ANCHOR = (23, 12)
STOCK_SHOP = "stock_village_shop"
STOCK_FARM = "stock_village_farm"
UNIT_PRICE = 10
HOLDING_CAP = 20
ISSUANCE = 100


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def make_engine(
    world_config: ParsedWorldConfig, scripts=None, wire_decisions: bool = False
) -> WorldEngine:
    engine = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    engine.action_service = ActionExecutionService(engine, SessionLocal)
    engine.economy_service = EconomyService(engine, SessionLocal)
    engine.stock_service = StockService(engine, SessionLocal)
    engine.god_action_service = GodActionService(engine, SessionLocal)
    engine.save_service = SaveService(engine, SessionLocal)
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


def stock_row(world_id: str, stock_id: str) -> Stock:
    session = SessionLocal()
    try:
        stock = session.get(Stock, {"world_id": world_id, "stock_id": stock_id})
        assert stock is not None
        return stock
    finally:
        session.close()


def holding_of(world_id: str, agent_id: str, stock_id: str) -> int:
    session = SessionLocal()
    try:
        holding = session.get(
            StockHolding,
            {"world_id": world_id, "agent_id": agent_id, "stock_id": stock_id},
        )
        return holding.shares if holding is not None else 0
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


def company_money(world_id: str, company_id: str) -> int:
    session = SessionLocal()
    try:
        company = session.get(Company, {"world_id": world_id, "company_id": company_id})
        assert company is not None
        return company.money
    finally:
        session.close()


def company_transactions(world_id: str, company_id: str) -> list[CompanyTransaction]:
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(CompanyTransaction)
                .where(
                    CompanyTransaction.world_id == world_id,
                    CompanyTransaction.company_id == company_id,
                )
                .order_by(CompanyTransaction.transaction_id)
            )
        )
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Seeded issues and subscriptions
# --------------------------------------------------------------------------- #


def test_seeded_issues_are_active_cooperative_shares(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    data = engine.stock_service.list_stocks(runtime.world_id)
    assert data is not None
    assert data["holdings"] == []
    assert [share["stock_id"] for share in data["stocks"]] == [STOCK_FARM, STOCK_SHOP]

    by_id = {share["stock_id"]: share for share in data["stocks"]}
    assert by_id[STOCK_FARM] == {
        "stock_id": STOCK_FARM,
        "name": "晨露农场合作社份额",
        "unit_price": UNIT_PRICE,
        "operating_volume": 0,
        "source": "job",
        "company_id": "job_farm_production",
        "issuer_company_id": "company_morning_farm",
        "outstanding_shares": ISSUANCE,
        "available_shares": ISSUANCE,
        "holding_cap": HOLDING_CAP,
    }
    assert by_id[STOCK_SHOP]["issuer_company_id"] == "company_village_shop"
    assert company_money(runtime.world_id, "company_morning_farm") == 800
    assert company_money(runtime.world_id, "company_village_shop") == 1000


def test_residents_start_with_the_cooperative_wallet(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    assert agent_money(runtime.world_id, "agent_linxia") == 600
    assert agent_money(runtime.world_id, "agent_touzi") == 600


def test_subscription_moves_funds_to_issuer_and_records_fixed_price(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id

    ok, envelope, reason = engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="支持商店"
    )

    assert ok is True
    assert reason is None
    assert envelope.type == "stock_bought"
    assert envelope.payload == {
        "agent_id": "agent_linxia",
        "stock_id": STOCK_SHOP,
        "stock_name": "晨露商店合作社份额",
        "shares": 2,
        "unit_price": UNIT_PRICE,
        "total": 20,
    }
    assert agent_money(world_id, "agent_linxia") == 580
    assert company_money(world_id, "company_village_shop") == 1020
    assert holding_of(world_id, "agent_linxia", STOCK_SHOP) == 2

    resident_txs = transaction_rows(engine, world_id, "agent_linxia")
    assert [(tx.type, tx.amount, tx.item_id, tx.quantity) for tx in resident_txs] == [
        ("coop_share_buy", -20, STOCK_SHOP, 2)
    ]
    issuer_tx = next(
        tx for tx in company_transactions(world_id, "company_village_shop")
        if tx.type == "share_issue"
    )
    assert (issuer_tx.amount, issuer_tx.quantity) == (20, 2)


def test_subscription_is_limited_by_resident_cap_and_total_issuance(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id

    assert engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=HOLDING_CAP, reason="认购"
    )[0]
    ok, envelope, reason = engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="再认购"
    )
    assert (ok, envelope, reason) == (False, None, MSG_HOLDING_CAP)

    for agent_id in ("agent_chenyu", "agent_laozhang", "agent_limujiang", "agent_sunshen"):
        assert engine.stock_service.buy_stock(
            world_id, agent_id, STOCK_SHOP, shares=HOLDING_CAP, reason="认购"
        )[0]
    assert holding_of(world_id, "agent_sunshen", STOCK_SHOP) == HOLDING_CAP

    ok, envelope, reason = engine.stock_service.buy_stock(
        world_id, "agent_zhoushen", STOCK_SHOP, shares=1, reason="认购"
    )
    assert (ok, envelope, reason) == (False, None, MSG_ISSUANCE_EXHAUSTED)
    listed = engine.stock_service.list_stocks(world_id)
    assert listed is not None
    assert next(s for s in listed["stocks"] if s["stock_id"] == STOCK_SHOP)["available_shares"] == 0


def test_subscription_rejects_insufficient_funds_busy_or_unknown_issue(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", money=5)

    assert engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="认购"
    ) == (False, None, "余额不足")
    set_agent(engine, world_id, "agent_linxia", action_type="work")
    assert engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="认购"
    ) == (False, None, "当前行动未完成")
    set_agent(engine, world_id, "agent_linxia", action_type=None)
    assert engine.stock_service.buy_stock(
        world_id, "agent_linxia", "missing", shares=1, reason="认购"
    ) == (False, None, MSG_STOCK_MISSING)


# --------------------------------------------------------------------------- #
# Issuer-funded redemption
# --------------------------------------------------------------------------- #


def test_redemption_returns_fixed_price_and_retires_holding(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    assert engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=3, reason="认购"
    )[0]

    ok, envelope, reason = engine.stock_service.sell_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="退出"
    )

    assert ok is True
    assert reason is None
    assert envelope.payload == {
        "agent_id": "agent_linxia",
        "stock_id": STOCK_SHOP,
        "stock_name": "晨露商店合作社份额",
        "shares": 2,
        "unit_price": UNIT_PRICE,
        "total": 20,
    }
    assert agent_money(world_id, "agent_linxia") == 590
    assert company_money(world_id, "company_village_shop") == 1010
    assert holding_of(world_id, "agent_linxia", STOCK_SHOP) == 1
    assert stock_row(world_id, STOCK_SHOP).price == UNIT_PRICE
    assert [tx.type for tx in transaction_rows(engine, world_id, "agent_linxia")] == [
        "coop_share_buy",
        "coop_share_sell",
    ]
    assert any(
        tx.type == "share_buyback"
        for tx in company_transactions(world_id, "company_village_shop")
    )


def test_redemption_requires_resident_holding_and_issuer_funds(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    assert engine.stock_service.sell_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="退出"
    ) == (False, None, MSG_NOT_ENOUGH_SHARES)

    assert engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="认购"
    )[0]
    session = SessionLocal()
    try:
        session.execute(
            update(Company)
            .where(
                Company.world_id == world_id,
                Company.company_id == "company_village_shop",
            )
            .values(money=0)
        )
        session.commit()
    finally:
        session.close()

    ok, envelope, reason = engine.stock_service.sell_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=1, reason="退出"
    )
    assert (ok, envelope) == (False, None)
    assert reason == "发行合作社资金不足，无法回购份额"
    assert holding_of(world_id, "agent_linxia", STOCK_SHOP) == 1


# --------------------------------------------------------------------------- #
# Operating volume and day boundary
# --------------------------------------------------------------------------- #


def test_operating_events_change_only_transparent_volume(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)

    for _ in range(2):
        ok, _, reason = engine.economy_service.buy(
            world_id, "agent_linxia", "bread", quantity=1, reason="买面包"
        )
        assert ok is True, reason

    share = stock_row(world_id, STOCK_SHOP)
    assert share.day_business == 2
    assert share.price == UNIT_PRICE
    volume_events = [
        event
        for event in engine.events_after(world_id, 0)
        if event.type == "stock_volume_changed" and event.payload["stock_id"] == STOCK_SHOP
    ]
    assert [event.payload["operating_volume"] for event in volume_events] == [1, 2]

    advance_minutes(engine, world_id, 61)
    assert stock_row(world_id, STOCK_SHOP).price == UNIT_PRICE
    assert not [
        event for event in engine.events_after(world_id, 0) if event.type == "stock_price_changed"
    ]


def test_day_boundary_resets_volume_without_dividend(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    place_agent(engine, world_id, "agent_linxia", "village_shop", *SHOP_ANCHOR)
    ok, _, reason = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=1, reason="买面包"
    )
    assert ok is True, reason

    advance_minutes(engine, world_id, 961)

    share = stock_row(world_id, STOCK_SHOP)
    assert share.day_business == 0
    assert share.price == UNIT_PRICE
    events = engine.events_after(world_id, 0)
    assert not [event for event in events if event.type == "dividend_paid"]
    assert any(
        event.type == "stock_volume_changed"
        and event.payload == {
            "stock_id": STOCK_SHOP,
            "stock_name": "晨露商店合作社份额",
            "operating_volume": 0,
        }
        for event in events
    )


# --------------------------------------------------------------------------- #
# Administrator price command and persistence
# --------------------------------------------------------------------------- #


def test_administrator_can_explicitly_change_fixed_unit_price(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    result = engine.god_action_service.apply(
        world_id,
        "change_stock_price",
        parameters={"stock_id": STOCK_SHOP, "price": 15},
        reason="测试",
    )

    assert result["success"] is True
    assert result["result"] == {"stock_id": STOCK_SHOP, "unit_price": 15}
    assert [event["type"] for event in result["events"]] == [
        "god_action_applied",
        "stock_price_changed",
    ]
    assert result["events"][1]["payload"] == {
        "stock_id": STOCK_SHOP,
        "stock_name": "晨露商店合作社份额",
        "unit_price": 15,
        "operating_volume": 0,
    }
    assert stock_row(world_id, STOCK_SHOP).price == 15

    with pytest.raises(HTTPException) as exc:
        engine.god_action_service.apply(
            world_id,
            "change_stock_price",
            parameters={"stock_id": STOCK_SHOP, "price": 0},
            reason="测试",
        )
    assert exc.value.status_code == 400


def test_save_restore_preserves_cooperative_issues_and_holdings(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    assert engine.stock_service.buy_stock(
        world_id, "agent_linxia", STOCK_SHOP, shares=2, reason="认购"
    )[0]
    saved = engine.save_service.save(world_id)
    assert saved is not None

    restored = engine.save_service.restore(saved.save_id)
    data = engine.stock_service.list_stocks(restored.world_id)
    assert data is not None
    shop = next(share for share in data["stocks"] if share["stock_id"] == STOCK_SHOP)
    assert shop["unit_price"] == UNIT_PRICE
    assert shop["available_shares"] == ISSUANCE - 2
    assert data["holdings"] == [
        {"agent_id": "agent_linxia", "stock_id": STOCK_SHOP, "shares": 2}
    ]

    session = SessionLocal()
    try:
        save = session.get(Save, saved.save_id)
        assert save is not None
        max_sequence = int((save.payload_json or {}).get("max_sequence") or 0)
    finally:
        session.close()
    assert engine.events_after(restored.world_id, max_sequence)


# --------------------------------------------------------------------------- #
# HTTP and scripted-agent contracts
# --------------------------------------------------------------------------- #


def test_http_cooperative_share_contract(client: TestClient) -> None:
    created = client.post("/api/worlds", json={"name": "份额 API", "autonomous": False})
    assert created.status_code == 201, created.text
    world_id = created.json()["world_id"]

    listed = client.get(f"/api/worlds/{world_id}/stocks")
    assert listed.status_code == 200
    body = listed.json()
    assert [share["stock_id"] for share in body["stocks"]] == [STOCK_FARM, STOCK_SHOP]
    shop = next(share for share in body["stocks"] if share["stock_id"] == STOCK_SHOP)
    assert shop["unit_price"] == UNIT_PRICE
    assert shop["available_shares"] == ISSUANCE
    assert shop["holding_cap"] == HOLDING_CAP
    assert body["holdings"] == []

    pin = client.post(
        f"/api/worlds/{world_id}/god-actions",
        json={
            "command_type": "deduct_money",
            "target_id": "agent_linxia",
            "parameters": {"amount": 580},
            "reason": "test",
        },
    )
    assert pin.status_code == 200, pin.text

    subscribed = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={
            "action_type": "buy_stock",
            "stock_id": STOCK_SHOP,
            "shares": 2,
            "reason": "test",
        },
    )
    assert subscribed.status_code == 200, subscribed.text
    assert subscribed.json()["event"]["type"] == "stock_bought"

    insufficient = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={
            "action_type": "buy_stock",
            "stock_id": STOCK_SHOP,
            "shares": 1,
            "reason": "test",
        },
    )
    assert insufficient.status_code == 409
    assert insufficient.json()["reason"] == "余额不足"

    listed = client.get(f"/api/worlds/{world_id}/stocks")
    assert listed.json()["holdings"] == [
        {"agent_id": "agent_linxia", "stock_id": STOCK_SHOP, "shares": 2}
    ]


def test_llm_script_can_subscribe_to_a_cooperative_share(world_config: ParsedWorldConfig) -> None:
    engine = make_engine(
        world_config,
        scripts={
            "agent_linxia": [
                ("buy_stock", {"stock_id": STOCK_SHOP, "shares": 2, "reason": "支持商店"})
            ]
        },
        wire_decisions=True,
    )
    runtime = engine.create_world("份额决策", autonomous=True)
    world_id = runtime.world_id

    for _ in range(3):
        advance_minutes(engine, world_id, 10)
        if holding_of(world_id, "agent_linxia", STOCK_SHOP) == 2:
            break
    assert holding_of(world_id, "agent_linxia", STOCK_SHOP) == 2
    assert agent_money(world_id, "agent_linxia") == 580
    engine._runtimes.clear()
