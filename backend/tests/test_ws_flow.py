"""HTTP + WebSocket flow tests: REST contract, speed validation, WS snapshot + increments."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

WORLD_ID_RE = re.compile(r"^world_\d{3,}$")


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def create_world(client: TestClient, name: str | None = None) -> dict:
    body = {"name": name} if name else None
    response = client.post("/api/worlds", json=body)
    assert response.status_code == 201, response.text
    data = response.json()
    assert re.match(WORLD_ID_RE, data["world_id"])
    return data


# --------------------------------------------------------------------------- #
# World CRUD
# --------------------------------------------------------------------------- #


def test_create_world_contract(client: TestClient) -> None:
    data = create_world(client, "API 世界")
    assert data["world_time"] == 480
    assert data["speed"] == 1
    assert data["paused"] is False

    listed = client.get("/api/worlds").json()
    assert any(w["world_id"] == data["world_id"] and w["name"] == "API 世界" for w in listed)

    detail = client.get(f"/api/worlds/{data['world_id']}").json()
    assert detail["world_id"] == data["world_id"]

    assert client.get("/api/worlds/does_not_exist").status_code == 404


def test_snapshot_shape(client: TestClient) -> None:
    data = create_world(client)
    payload = client.get(f"/api/worlds/{data['world_id']}/snapshot").json()
    assert set(payload["world"]) >= {"world_id", "world_time", "speed", "paused", "weather", "day"}
    assert payload["world"]["day"] == 1
    assert len(payload["agents"]) == 9
    for agent in payload["agents"]:
        assert set(agent) >= {
            "agent_id", "name", "col", "row", "location_id", "satiety", "energy", "money", "action",
        }
    assert len(payload["locations"]) == 15
    assert payload["latest_sequence"] >= 0


# --------------------------------------------------------------------------- #
# Clock control
# --------------------------------------------------------------------------- #


def test_speed_validation(client: TestClient) -> None:
    data = create_world(client)
    world_id = data["world_id"]

    assert client.post(f"/api/worlds/{world_id}/speed", json={"speed": 3}).status_code == 422
    assert client.post(f"/api/worlds/{world_id}/speed", json={"speed": 0}).status_code == 422
    assert client.post(f"/api/worlds/{world_id}/speed", json={"speed": 2}).json() == {"ok": True}
    assert client.post(f"/api/worlds/{world_id}/speed", json={"speed": 5}).json() == {"ok": True}
    assert client.post(f"/api/worlds/{world_id}/speed", json={"speed": 10}).json() == {"ok": True}
    snapshot = client.get(f"/api/worlds/{world_id}/snapshot").json()
    assert snapshot["world"]["speed"] == 10
    # speed changes are persisted in the event log, with no sequence gaps
    events = client.get(f"/api/worlds/{world_id}/events?after_sequence=0").json()
    types = [e["type"] for e in events]
    assert "world_speed_changed" in types
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(sequences) + 1))


def test_pause_resume_idempotent(client: TestClient) -> None:
    data = create_world(client)
    world_id = data["world_id"]

    assert client.post(f"/api/worlds/{world_id}/pause").json() == {"ok": True}
    assert client.post(f"/api/worlds/{world_id}/pause").json() == {"ok": True}  # idempotent
    assert client.get(f"/api/worlds/{world_id}/snapshot").json()["world"]["paused"] is True

    assert client.post(f"/api/worlds/{world_id}/resume").json() == {"ok": True}
    assert client.get(f"/api/worlds/{world_id}/snapshot").json()["world"]["paused"] is False


def test_pause_rejects_move_then_resume_allows(client: TestClient) -> None:
    data = create_world(client)
    world_id = data["world_id"]

    client.post(f"/api/worlds/{world_id}/pause")
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={"action_type": "move", "destination_id": "village_shop"},
    )
    assert response.status_code == 409
    assert response.json() == {"success": False, "reason": "世界已暂停"}

    client.post(f"/api/worlds/{world_id}/resume")
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={"action_type": "move", "destination_id": "village_shop"},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


# --------------------------------------------------------------------------- #
# Action API
# --------------------------------------------------------------------------- #


def test_action_rejections(client: TestClient) -> None:
    data = create_world(client)
    world_id = data["world_id"]

    # missing destination id for a move -> 422 (schema)
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={"action_type": "move"},
    )
    assert response.status_code == 422

    # unknown destination -> 409 rule violation
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={"action_type": "move", "destination_id": "nowhere"},
    )
    assert response.status_code == 409
    assert response.json()["reason"] == "目标地点不存在"

    # busy agent -> 409
    ok = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={"action_type": "move", "destination_id": "village_shop"},
    )
    assert ok.status_code == 200
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={"action_type": "move", "destination_id": "village_plaza"},
    )
    assert response.status_code == 409
    assert response.json()["reason"] == "当前行动未完成"

    # wait with negative minutes -> 422
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_zhangming/actions",
        json={"action_type": "wait", "minutes": -5},
    )
    assert response.status_code == 422

    # a different agent can wait while linxia moves
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_zhangming/actions",
        json={"action_type": "wait", "minutes": 20, "reason": "站岗"},
    )
    assert response.status_code == 200
    assert response.json()["event"]["type"] == "agent_wait_started"


def test_events_after_sequence_endpoint(client: TestClient) -> None:
    data = create_world(client)
    world_id = data["world_id"]
    client.post(
        f"/api/worlds/{world_id}/agents/agent_zhangming/actions",
        json={"action_type": "move", "destination_id": "village_plaza"},
    )
    events = client.get(f"/api/worlds/{world_id}/events?after_sequence=0").json()
    assert isinstance(events, list)
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)
    move_started = [e for e in events if e["type"] == "agent_move_started"]
    assert move_started and move_started[0]["payload"]["agent_id"] == "agent_zhangming"
    # gap recovery: after the move_started sequence only newer events remain
    after = client.get(
        f"/api/worlds/{world_id}/events?after_sequence={move_started[0]['sequence']}"
    ).json()
    assert all(e["sequence"] > move_started[0]["sequence"] for e in after)


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #


def test_ws_snapshot_then_move_event(client: TestClient) -> None:
    data = create_world(client)
    world_id = data["world_id"]

    with client.websocket_connect(f"/ws/worlds/{world_id}") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "world_snapshot"
        assert snapshot["world_id"] == world_id
        assert len(snapshot["payload"]["agents"]) == 9
        assert snapshot["payload"]["latest_sequence"] == snapshot["sequence"]

        # Trigger a manual move over REST; the started event must stream over WS.
        response = client.post(
            f"/api/worlds/{world_id}/agents/agent_linxia/actions",
            json={"action_type": "move", "destination_id": "village_shop"},
        )
        assert response.status_code == 200

        found = None
        for _ in range(60):
            message = ws.receive_json()
            if message["type"] == "agent_move_started":
                found = message
                break
        assert found is not None, "agent_move_started never arrived over WS"
        assert found["payload"]["agent_id"] == "agent_linxia"
        assert found["payload"]["to"] == [23, 12]
        assert found["sequence"] > snapshot["sequence"]


def test_ws_unknown_world_closes(client: TestClient) -> None:
    with client.websocket_connect("/ws/worlds/does_not_exist") as ws:
        with pytest.raises(Exception):
            ws.receive_json()
