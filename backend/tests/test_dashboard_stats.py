"""数据看板 tests: LLM、事件和运营总览统计端点。

Empty-state contracts, seeded aggregation, dashboard operational totals, and
404 handling for unknown worlds — all through TestClient.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config.gameplay import (
    HUNGER_FORCED_EAT_THRESHOLD,
    LONELINESS_BOOST_THRESHOLD,
    LOW_ENERGY_ACTION_THRESHOLD,
    MOOD_BOOST_THRESHOLD,
)
from app.config.settings import Settings
from app.database.models.agents import Agent
from app.database.models.companies import WorkShift
from app.database.models.llm_runs import LLMRun
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.main import app

EMPTY_LLM = {
    "total_calls": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "failed_calls": 0,
    "error_rate": 0.0,
    "avg_latency_ms": 0,
    "by_agent": [],
    "by_model": [],
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_event_stats_endpoint(client: TestClient) -> None:
    created = client.post("/api/worlds", json={"name": "看板事件统计"})
    assert created.status_code == 201, created.text
    world_id = created.json()["world_id"]

    first = client.get(f"/api/worlds/{world_id}/stats/events")
    assert first.status_code == 200
    body = first.json()
    assert set(body) == {"total", "latest_sequence", "by_type"}
    assert body["total"] == sum(row["count"] for row in body["by_type"])
    assert body["latest_sequence"] == body["total"]

    acted = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={"action_type": "wait", "minutes": 1, "reason": "看板测试"},
    )
    assert acted.status_code == 200, acted.text

    second = client.get(f"/api/worlds/{world_id}/stats/events")
    assert second.status_code == 200
    after = second.json()
    assert after["total"] >= 1
    assert after["total"] == sum(row["count"] for row in after["by_type"])
    assert after["total"] > body["total"]
    assert after["latest_sequence"] > body["latest_sequence"]

    missing = client.get("/api/worlds/does_not_exist/stats/events")
    assert missing.status_code == 404


def test_llm_stats_endpoint(client: TestClient) -> None:
    created = client.post("/api/worlds", json={"name": "看板LLM统计"})
    assert created.status_code == 201, created.text
    world_id = created.json()["world_id"]

    empty = client.get(f"/api/worlds/{world_id}/stats/llm")
    assert empty.status_code == 200
    assert empty.json() == EMPTY_LLM

    session = SessionLocal()
    try:
        session.add_all(
            [
                LLMRun(
                    world_id=world_id,
                    agent_id="agent_linxia",
                    world_time=600,
                    model="m1",
                    input_tokens=100,
                    output_tokens=10,
                    latency_ms=800,
                    success=True,
                    tool_name="wait",
                ),
                LLMRun(
                    world_id=world_id,
                    agent_id="agent_linxia",
                    world_time=620,
                    model="m1",
                    input_tokens=200,
                    output_tokens=20,
                    latency_ms=1200,
                    success=False,
                    error_type="timeout",
                    tool_name="wait",
                ),
                LLMRun(
                    world_id=world_id,
                    agent_id="agent_zhangming",
                    world_time=640,
                    model="m2",
                    input_tokens=300,
                    output_tokens=30,
                    latency_ms=1000,
                    success=True,
                    tool_name="wait",
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    stats = client.get(f"/api/worlds/{world_id}/stats/llm")
    assert stats.status_code == 200
    body = stats.json()
    assert body["total_calls"] == 3
    assert body["total_input_tokens"] == 600
    assert body["total_output_tokens"] == 60
    assert body["failed_calls"] == 1
    assert body["error_rate"] == round(1 / 3, 4)
    assert body["avg_latency_ms"] == 1000

    by_agent = {row["agent_id"]: row for row in body["by_agent"]}
    assert by_agent["agent_linxia"]["calls"] == 2
    assert by_agent["agent_linxia"]["failed"] == 1
    assert by_agent["agent_linxia"]["input_tokens"] == 300
    assert by_agent["agent_linxia"]["output_tokens"] == 30
    assert by_agent["agent_zhangming"]["calls"] == 1
    assert by_agent["agent_zhangming"]["failed"] == 0

    by_model = {row["model"]: row for row in body["by_model"]}
    assert by_model["m1"]["calls"] == 2
    assert by_model["m1"]["input_tokens"] == 300
    assert by_model["m1"]["output_tokens"] == 30
    assert by_model["m2"]["calls"] == 1
    assert by_model["m2"]["input_tokens"] == 300
    assert by_model["m2"]["output_tokens"] == 30

    missing = client.get("/api/worlds/does_not_exist/stats/llm")
    assert missing.status_code == 404


def _create_overview_world(client: TestClient) -> str:
    created = client.post("/api/worlds", json={"name": "看板运营总览"})
    assert created.status_code == 201, created.text
    world_id = created.json()["world_id"]
    day_start = 2 * 1440

    session = SessionLocal()
    try:
        world = session.get(World, world_id)
        assert world is not None
        world.world_time = day_start + 600
        world.treasury = 830
        world.public_work_budget_remaining = 275
        world.public_work_escrow = 125

        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id).order_by(Agent.agent_id)
        ).all()
        assert len(agents) >= 2
        for agent in agents:
            agent.daily_call_count = 0
            agent.daily_token_usage = 0
            agent.is_deciding = False
        agents[0].daily_call_count = 3
        agents[0].daily_token_usage = 700
        agents[0].is_deciding = True
        agents[1].daily_call_count = 2
        agents[1].daily_token_usage = 450

        session.add_all(
            [
                WorkShift(
                    world_id=world_id,
                    employment_id="overview-normal",
                    company_id="overview-company",
                    position_id="overview-position",
                    agent_id=agents[0].agent_id,
                    scheduled_start=day_start + 100,
                    scheduled_end=day_start + 220,
                    actual_start=day_start + 100,
                    actual_end=day_start + 220,
                    status="completed",
                ),
                WorkShift(
                    world_id=world_id,
                    employment_id="overview-late",
                    company_id="overview-company",
                    position_id="overview-position",
                    agent_id=agents[1].agent_id,
                    scheduled_start=day_start + 240,
                    scheduled_end=day_start + 360,
                    actual_start=day_start + 255,
                    actual_end=day_start + 360,
                    status="completed",
                    late_minutes=15,
                ),
                WorkShift(
                    world_id=world_id,
                    employment_id="overview-absent",
                    company_id="overview-company",
                    position_id="overview-position",
                    agent_id=agents[0].agent_id,
                    scheduled_start=day_start + 400,
                    scheduled_end=day_start + 520,
                    status="absent",
                ),
                WorkShift(
                    world_id=world_id,
                    employment_id="overview-previous-day",
                    company_id="overview-company",
                    position_id="overview-position",
                    agent_id=agents[1].agent_id,
                    scheduled_start=day_start - 120,
                    scheduled_end=day_start - 1,
                    status="absent",
                ),
            ]
        )
        session.commit()
    finally:
        session.close()
    return world_id


def test_dashboard_overview_stats_with_budget(
        client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.worlds.get_settings",
        lambda: Settings(world_daily_token_budget=1000),
    )
    world_id = _create_overview_world(client)

    response = client.get(f"/api/worlds/{world_id}/stats/overview")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "treasury": {
            "balance": 830,
            "public_work_budget_remaining": 275,
            "public_work_escrow": 125,
        },
        "attendance_today": {"attended": 2, "late": 1, "absent": 1},
        "llm_today": {
            "calls": 5,
            "token_usage": 1150,
            "token_budget": 1000,
            "token_remaining": 0,
            "deciding_agents": 1,
        },
        "need_thresholds": {
            "satiety_lte": HUNGER_FORCED_EAT_THRESHOLD,
            "energy_lte": LOW_ENERGY_ACTION_THRESHOLD,
            "mood_lte": MOOD_BOOST_THRESHOLD,
            "loneliness_gte": LONELINESS_BOOST_THRESHOLD,
        },
    }


def test_dashboard_overview_stats_unlimited_budget(
        client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.worlds.get_settings",
        lambda: Settings(world_daily_token_budget=0),
    )
    world_id = _create_overview_world(client)

    response = client.get(f"/api/worlds/{world_id}/stats/overview")
    assert response.status_code == 200, response.text
    llm_today = response.json()["llm_today"]
    assert llm_today["calls"] == 5
    assert llm_today["token_usage"] == 1150
    assert llm_today["token_budget"] is None
    assert llm_today["token_remaining"] is None


def test_dashboard_overview_stats_unknown_world(client: TestClient) -> None:
    response = client.get("/api/worlds/does_not_exist/stats/overview")
    assert response.status_code == 404
    assert response.json() == {"detail": "世界不存在"}
