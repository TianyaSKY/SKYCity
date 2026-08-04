"""WebSocket route: one envelope stream per world.

On connect the server sends a single world_snapshot envelope with the full
state, then streams increments. Inbound messages are ignored (keep-alive /
future command channel); the connection is torn down defensively.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from app.world_engine.engine import WorldEngine

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/worlds/{world_id}")
async def world_websocket(websocket: WebSocket, world_id: str) -> None:
    """Stream events for one world; snapshot first, increments after."""
    await websocket.accept()
    engine: WorldEngine = websocket.app.state.engine
    runtime = engine.get_runtime(world_id)
    if runtime is None:
        await websocket.close(code=4404)  # world not found
        return
    envelope = engine.snapshot_envelope(world_id)
    if envelope is None:
        await websocket.close(code=4404)
        return
    try:
        await websocket.send_json(envelope)
        runtime.ws_clients.add(websocket)
        while True:
            # Ignore inbound payloads in M2; receiving keeps the socket alive
            # and surfaces client disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass
    except Exception:  # noqa: BLE001 - never crash the app on a bad socket
        logger.exception("WebSocket error for world {}", world_id)
    finally:
        runtime.ws_clients.discard(websocket)
