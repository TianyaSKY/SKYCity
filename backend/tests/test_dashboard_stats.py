"""数据看板 tests: the /stats/llm and /stats/events aggregation endpoints.

Empty-state contracts, seeded LLMRun aggregation, event totals growing on
new actions, and 404 handling for unknown worlds — all through TestClient.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database.models.llm_runs import LLMRun
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
