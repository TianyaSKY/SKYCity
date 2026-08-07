"""M11 tests: agent-to-agent transfers & item gifts — rule gates (R19.1
initiator idle + target within 3 cells, R19.2 no credit / no over-giving /
no self transfers), ledger + event contracts, the HTTP contract and the LLM
transfer_money tool.

Drives the WorldEngine directly (no HTTP, no background loop) exactly like
test_stocks.py, plus one TestClient block for the REST contracts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.session import SessionLocal
from app.main import app
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.conversation_service import MSG_NOT_NEAR, MSG_TARGET_MISSING
from app.services.economy_service import (
    MSG_BUSY,
    MSG_NO_MONEY,
    MSG_NOT_IN_INVENTORY,
    EconomyService,
)
from app.services.god_action_service import GodActionService
from app.services.save_service import SaveService
from app.services.transfer_service import MSG_SELF_TRANSFER, TransferService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_economy import add_inventory, inventory_of, place_agent, set_agent, transaction_rows
from tests.test_world_engine import advance_minutes

PLAZA_ANCHOR = (32, 20)


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


def park_both(engine: WorldEngine, world_id: str) -> None:
    """Park 林夏 + 张明 on the same plaza cell (distance 0, within R9)."""
    place_agent(engine, world_id, "agent_linxia", "village_plaza", *PLAZA_ANCHOR)
    place_agent(engine, world_id, "agent_zhangming", "village_plaza", *PLAZA_ANCHOR)


# --------------------------------------------------------------------------- #
# Money transfer (R19)
# --------------------------------------------------------------------------- #


def test_transfer_success(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    park_both(engine, world_id)

    ok, envelope, reason = engine.transfer_service.transfer_money(
        world_id, "agent_linxia", "agent_zhangming", amount=30, reason="还你钱"
    )
    assert ok is True and reason is None
    assert envelope.type == "money_transferred"
    assert envelope.payload == {
        "from_agent_id": "agent_linxia",
        "to_agent_id": "agent_zhangming",
        "amount": 30,
        "reason": "还你钱",
    }
    assert agent_row_money(engine, world_id, "agent_linxia") == 20  # 50 - 30
    assert agent_row_money(engine, world_id, "agent_zhangming") == 80  # 50 + 30

    types = [e.type for e in engine.events_after(world_id, 0)]
    assert "money_transferred" in types
    moved = [
        e
        for e in engine.events_after(world_id, 0)
        if e.type == "money_changed" and e.payload.get("amount") in (-30, 30)
    ]
    assert len(moved) == 2, "both sides must get their money_changed"
    assert sorted(e.payload["balance"] for e in moved) == [20, 80]

    sender_txs = transaction_rows(engine, world_id, "agent_linxia")
    assert len(sender_txs) == 1
    assert sender_txs[0].type == "transfer"
    assert sender_txs[0].amount == -30
    assert sender_txs[0].balance_after == 20
    recipient_txs = transaction_rows(engine, world_id, "agent_zhangming")
    assert len(recipient_txs) == 1
    assert recipient_txs[0].type == "transfer"
    assert recipient_txs[0].amount == 30
    assert recipient_txs[0].balance_after == 80


def test_transfer_insufficient(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    park_both(engine, world_id)
    set_agent(engine, world_id, "agent_linxia", money=5)

    ok, envelope, reason = engine.transfer_service.transfer_money(
        world_id, "agent_linxia", "agent_zhangming", amount=10, reason="借钱"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NO_MONEY  # R7: no credit
    assert agent_row_money(engine, world_id, "agent_linxia") == 5
    assert agent_row_money(engine, world_id, "agent_zhangming") == 50
    types = [e.type for e in engine.events_after(world_id, 0)]
    assert "money_transferred" not in types


def test_transfer_too_far(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    # Spawn cells: 林夏 (18,27) vs 张明 (40,11) -> manhattan 38 > 3 (R9).
    ok, envelope, reason = engine.transfer_service.transfer_money(
        runtime.world_id, "agent_linxia", "agent_zhangming", amount=10, reason="测试"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_NEAR


def test_transfer_to_self(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.transfer_service.transfer_money(
        runtime.world_id, "agent_linxia", "agent_linxia", amount=10, reason="测试"
    )
    assert ok is False and envelope is None
    assert reason == MSG_SELF_TRANSFER


def test_transfer_busy(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    set_agent(engine, world_id, "agent_linxia", action_type="work")

    ok, envelope, reason = engine.transfer_service.transfer_money(
        world_id, "agent_linxia", "agent_zhangming", amount=10, reason="测试"
    )
    assert ok is False and envelope is None
    assert reason == MSG_BUSY  # R1: the initiator must be idle


def test_transfer_unknown_target(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.transfer_service.transfer_money(
        runtime.world_id, "agent_linxia", "agent_nobody", amount=10, reason="测试"
    )
    assert ok is False and envelope is None
    assert reason == MSG_TARGET_MISSING


# --------------------------------------------------------------------------- #
# Item gift (R19)
# --------------------------------------------------------------------------- #


def test_give_item_success(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    park_both(engine, world_id)
    add_inventory(engine, world_id, "agent_linxia", "bread", 3)

    ok, envelope, reason = engine.transfer_service.give_item(
        world_id, "agent_linxia", "agent_zhangming", "bread", quantity=2, reason="送你面包"
    )
    assert ok is True and reason is None
    assert envelope.type == "item_given"
    assert envelope.payload == {
        "from_agent_id": "agent_linxia",
        "to_agent_id": "agent_zhangming",
        "item_id": "bread",
        "item_name": "面包",
        "quantity": 2,
        "reason": "送你面包",
    }
    assert inventory_of(engine, world_id, "agent_linxia") == {"bread": 1}
    assert inventory_of(engine, world_id, "agent_zhangming") == {"bread": 2}

    types = [e.type for e in engine.events_after(world_id, 0)]
    assert "item_given" in types
    changed = [
        e
        for e in engine.events_after(world_id, 0)
        if e.type == "inventory_changed"
    ]
    assert len(changed) == 2, "both sides must get their inventory_changed"

    sender_txs = transaction_rows(engine, world_id, "agent_linxia")
    assert len(sender_txs) == 1
    assert sender_txs[0].type == "item_gift"
    assert sender_txs[0].amount == 0
    assert sender_txs[0].item_id == "bread"
    assert sender_txs[0].quantity == 2
    recipient_txs = transaction_rows(engine, world_id, "agent_zhangming")
    assert len(recipient_txs) == 1
    assert recipient_txs[0].type == "item_gift"
    assert recipient_txs[0].amount == 0


def test_give_item_not_enough(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    park_both(engine, world_id)  # no bread in 林夏's backpack

    ok, envelope, reason = engine.transfer_service.give_item(
        world_id, "agent_linxia", "agent_zhangming", "bread", quantity=1, reason="测试"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_IN_INVENTORY
    assert inventory_of(engine, world_id, "agent_zhangming") == {}
    types = [e.type for e in engine.events_after(world_id, 0)]
    assert "item_given" not in types


# --------------------------------------------------------------------------- #
# HTTP contract (TestClient)
# --------------------------------------------------------------------------- #


def test_http_contract(client: TestClient) -> None:
    created = client.post("/api/worlds", json={"name": "转账API", "autonomous": False})
    assert created.status_code == 201, created.text
    world_id = created.json()["world_id"]

    for target in ("agent_linxia", "agent_zhangming"):
        teleport = client.post(
            f"/api/worlds/{world_id}/god-actions",
            json={
                "command_type": "teleport",
                "target_id": target,
                "parameters": {"location_id": "village_plaza"},
                "reason": "t",
            },
        )
        assert teleport.status_code == 200, teleport.text

    transferred = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={
            "action_type": "transfer_money",
            "target_agent_id": "agent_zhangming",
            "amount": 30,
            "reason": "test",
        },
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["event"]["type"] == "money_transferred"

    short = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={
            "action_type": "transfer_money",
            "target_agent_id": "agent_zhangming",
            "amount": 100,
            "reason": "test",
        },
    )
    assert short.status_code == 409
    assert short.json()["reason"] == "余额不足"

    spawned = client.post(
        f"/api/worlds/{world_id}/god-actions",
        json={
            "command_type": "spawn_item",
            "target_id": "agent_linxia",
            "parameters": {"item_id": "bread", "quantity": 2},
            "reason": "t",
        },
    )
    assert spawned.status_code == 200, spawned.text

    given = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={
            "action_type": "give_item",
            "target_agent_id": "agent_zhangming",
            "item_id": "bread",
            "quantity": 2,
            "reason": "test",
        },
    )
    assert given.status_code == 200, given.text
    assert given.json()["event"]["type"] == "item_given"


# --------------------------------------------------------------------------- #
# LLM scripted decision (transfer_money tool through the decision service)
# --------------------------------------------------------------------------- #


def test_llm_script_transfer(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(
        world_config,
        scripts={
            "agent_linxia": [
                ("transfer_money", {"target_agent_id": "agent_zhangming", "amount": 30, "reason": "还你钱"})
            ]
        },
        wire_decisions=True,
    )
    runtime = eng.create_world("转账决策", autonomous=True)
    world_id = runtime.world_id
    park_both(eng, world_id)

    done = False
    for _ in range(3):
        advance_minutes(eng, world_id, 10)
        if agent_row_money(eng, world_id, "agent_linxia") == 20:
            done = True
            break
    assert done, "scripted transfer_money decision did not execute"
    assert agent_row_money(eng, world_id, "agent_linxia") == 20  # 50 - 30
    assert agent_row_money(eng, world_id, "agent_zhangming") == 80  # 50 + 30
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
